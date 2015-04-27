"""
Distributed Locking Queue for Redis adapted from the Redlock algorithm.
"""
import logging
import random
import sys
import time
import redis
import threading
from concurrent.futures import ThreadPoolExecutor
from itertools import chain

from . import util
from . import exceptions

log = logging.getLogger('redis.lockingqueue')


# Lua scripts that are sent to redis
SCRIPTS = dict(
    # keys:
    # h_k = ordered hash of key in form:  priority:insert_time_since_epoch:key
    # Q = sorted set of queued items, h_k
    #
    # args:
    # expireat = seconds_since_epoch, presumably in the future
    # client_id = unique owner of the lock
    # randint = a random integer that changes every time script is called

    # returns 1 if got an item, and returns an error otherwise
    lq_get=dict(keys=('Q', ), args=('client_id', 'expireat'), script="""
local h_k = redis.call("ZRANGE", KEYS[1], 0, 0)[1]
if nil == h_k then return {err="queue empty"} end
if 1 ~= redis.call("SETNX", h_k, ARGV[1]) then
  return {err="already locked"} end
if 1 ~= redis.call("EXPIREAT", h_k, ARGV[2]) then
  return {err="invalid expireat"} end
redis.call("ZINCRBY", KEYS[1], 1, h_k)
return h_k
"""),

    # returns 1 if got lock. Returns an error otherwise
    lq_lock=dict(
        keys=('h_k', 'Q'), args=('expireat', 'randint', 'client_id'), script="""
if 0 == redis.call("SETNX", KEYS[1], ARGV[3]) then  -- did not get lock
  if redis.call("GET", KEYS[1]) == "completed" then
    redis.call("ZREM", KEYS[2], KEYS[1])
    return {err="already completed"}
  else
    local score = redis.call("ZSCORE", KEYS[2], KEYS[1])
    math.randomseed(tonumber(ARGV[2]))
    local num = math.random(math.floor(score) + 1)
    if num ~= 1 then
      redis.call("ZINCRBY", KEYS[2], (num-1)/score, KEYS[1])
    end
    return {err="already locked"}
  end
else
  redis.call("EXPIREAT", KEYS[1], ARGV[1])
  redis.call("ZINCRBY", KEYS[2], 1, KEYS[1])
  return 1
end
"""),

    # return 1 if extended lock.  Returns an error otherwise.
    # otherwise
    lq_extend_lock=dict(
        keys=('h_k', ), args=('expireat', 'client_id'), script="""
local rv = redis.call("GET", KEYS[1])
if ARGV[2] == rv then
    redis.call("EXPIREAT", KEYS[1], ARGV[1])
    return 1
elseif "completed" == rv then return {err="already completed"}
else return {err="expired"} end
"""),

    # returns 1 if removed, 0 if key was already removed.
    lq_consume=dict(
        keys=('h_k', 'Q'), args=('client_id', ), script="""
local rv = redis.pcall("GET", KEYS[1])
if ARGV[1] == rv or "completed" == rv then
  redis.call("SET", KEYS[1], "completed")
  redis.call("PERSIST", KEYS[1])  -- or EXPIRE far into the future...
  redis.call("ZREM", KEYS[2], KEYS[1])
  return 1
else return 0 end
"""),

    # returns 1 if removed, 0 otherwise
    lq_unlock=dict(
        keys=('h_k', ), args=('client_id', ), script="""
if ARGV[1] == redis.call("GET", KEYS[1]) then
    return(redis.call("DEL", KEYS[1]))
else
    return(0)
end
"""),

    # returns number of items in queue currently being processed
    # O(n)  -- eek!
    lq_qsize=dict(
        keys=('Q', ), args=(), script="""
local taken = 0
local queued = 0
for _,k in ipairs(redis.call("ZRANGE", KEYS[1], 0, -1)) do
  local v = redis.call("GET", k)
  if v then taken = taken + 1
  else queued = queued + 1 end
end
return {taken, queued}
"""),
)


class LockingQueue(object):
    """
    A Distributed Locking Queue implementation for Redis.

    The queue expects to receive at least 1 redis.StrictRedis client,
    where each client is connected to a different Redis server.
    When instantiating this class, if you do not ensure that the number
    of servers defined is always constant, you risk the possibility that
    the same lock may be obtained multiple times.
    """

    def __init__(self, queue_path, clients, n_servers, timeout=5,
                 Timer=threading.Timer,
                 map_async=ThreadPoolExecutor(sys.maxsize).map):
        """
        `queue_path` - a Redis key specifying where the queued items are
        `clients` - a list of redis.StrictRedis clients,
            each connected to a different Redis server
        `n_servers` - the number of Redis servers in your cluster
            (whether or not you have a client connected to it)
        `timeout` - number of seconds after which the lock is invalid.
            Increase if you have large network delays or long periods where
            your python code is paused while running long-running C code
        `Timer` - implements the threading.Timer api.  If you do not with to
            use Python's threading module, pass in something else here.
        `map_async` - a function of form map(func, iterable) that maps func on
            iterable sequence.
            By default, use concurrent.futures.ThreadPoolmap_async api
            If you don't want to use threads, pass in your own function
        """
        if len(clients) < n_servers // 2 + 1:
            raise exceptions.CannotObtainLock(
                "Must connect to at least half of the redis servers to"
                " obtain majority")
        self._map_async = map_async
        self._Timer = Timer
        self._polling_interval = timeout / 5.
        self._clock_drift = 0  # TODO
        self._clients = clients
        self._timeout = timeout
        self._n_servers = float(n_servers)
        self._params = dict(
            Q=queue_path,
            client_id=random.randint(0, sys.maxsize),
        )

    def size(self, queued=True, taken=True):
        """
        Return the approximate number of items in the queue, across all servers

        `queued` - number of items in queue that aren't being processed
        `taken` - number of items in queue that are currently being processed

        Because we cannot lock all redis servers at the same time and we don't
        store a lock/unlock history, we cannot get the exact number of items in
        the queue at a specific time.

        If you change the default parameters (taken=True, queued=True), the
        time complexity increases from O(log(n)) to O(n).
        """
        if not queued and not taken:
            raise UserWarning("Queued and taken cannot both be False")
        if taken and queued:
            def maybe_card(cli):
                try:
                    return cli.zcard(self._params['Q'])
                except redis.RedisError as err:
                    log.debug(
                        "Redis Error: %s" % err, extra=dict(
                            error=err, error_type=type(err).__name__,
                            redis_client=cli))
                    return 0
            return max(self._map_async(
                maybe_card, self._clients))

        taken_queued_counts = (x[1] for x in util.run_script(
            SCRIPTS, self._map_async,
            'lq_qsize', self._clients, **(self._params))
            if not isinstance(x[1], Exception))
        if taken and not queued:
            return max(x[0] for x in taken_queued_counts)
        if queued and not taken:
            return max(x[1] for x in taken_queued_counts)

    def extend_lock(self, h_k):
        """
        If you have received an item from the queue and wish to hold the lock
        on it for an amount of time close to or longer than the timeout, you
        must extend the lock!

        Returns one of the following:
            -1 if a redis server reported that the item is completed
            0 if otherwise failed to extend_lock
            number of seconds since epoch in the future when lock will expire
        """
        _, t_expireat = util.get_expireat(self._timeout)
        locks = list(util.run_script(
            SCRIPTS, self._map_async, 'lq_extend_lock', self._clients,
            h_k=h_k, expireat=t_expireat, **(self._params)))
        if not self._verify_not_already_completed(locks, h_k):
            return -1
        if not self._have_majority(locks, h_k):
            return 0
        return util.lock_still_valid(
            t_expireat, self._clock_drift, self._polling_interval)

    def consume(self, h_k):
        """Remove item from queue.  Return the percentage of servers we've
        successfully removed item on.

        If the returned value is < 50%, a minority of servers know that the
        item was consumed.  The the item could get locked again
        if this minority of servers is entirely unavailable while another
        client is getting items from the queue.

        You choose whether a return value < 50% is a failure.  You can also
        try to consume the same item twice.
        """
        clients = self._clients
        n_success = sum(
            x[1] for x in util.run_script(
                SCRIPTS, self._map_async,
                'lq_consume', clients, h_k=h_k, **self._params)
            if not isinstance(x[1], Exception)
        )
        if n_success == 0:
            raise exceptions.ConsumeError(
                "Failed to mark the item as completed on any redis server")
        return 100. * n_success / self._n_servers

    def put(self, item, priority=100):
        """
        Put item onto queue.  Priority defines whether to prioritize
        getting this item off the queue before other items.
        Priority is not guaranteed
        """
        h_k = "%d:%f:%s" % (priority, time.time(), item)
        cnt = 0.
        for cli in self._clients:
            try:
                cnt += cli.zadd(self._params['Q'], 0, h_k)
            except redis.RedisError as err:
                log.warning(
                    "Could not put item onto a redis server.", extra=dict(
                        error=err, error_type=type(err).__name__,
                        redis_client=cli))
                continue
        return cnt / self._n_servers

    def get(self, extend_lock=True, check_all_servers=True):
        """
        Attempt to get an item from queue and obtain a lock on it to
        guarantee nobody else has a lock on this item.

        Returns an (item, h_k) or None.  An empty return value does
        not necessarily mean the queue is (or was) empty, though it's probably
        nearly empty.  `h_k` uniquely identifies the queued item

        `extend_lock` - If True, extends the lock indefinitely in the
            background until the lock is explicitly consumed or
            we can no longer extend the lock.
            If False, you need to set a very large timeout or call
            extend_lock() before the lock times out.
        `check_all_servers` - If True, query all redis servers for an item.
            Attempt to obtain the lock on the first item received.
            If False, query only 1 redis server for an item and attempt to
            obtain a lock on it.  If False and one of the servers is not
            reachable, the min. chance you will get nothing from the queue is
            1 / n_servers.  If True, we always preference the fastest response.
        """
        t_start, t_expireat = util.get_expireat(self._timeout)
        client, h_k = self._get_candidate_keys(t_expireat, check_all_servers)
        if not h_k:
            return
        if self._acquire_lock_majority(client, h_k, t_start, t_expireat):
            if extend_lock:
                util.continually_extend_lock_in_background(
                    h_k, self.extend_lock, self._polling_interval, self._Timer)
            priority, insert_time, item = h_k.decode().split(':', 2)
            return item, h_k

    def _get_candidate_keys(self, t_expireat, check_all_servers):
        """Choose one server to get an item from.  Return (client, key)

        If `check_all_servers` is True, use the results from the first server
        to that returns an item.  This could be dangerous because it
        preferences the fastest server.  If the slowest server for some reason
        had keys that other servers didn't have, these keys would be less likely
        to get synced to the other servers.
        """
        if check_all_servers:
            clis = list(self._clients)
            random.shuffle(clis)
        else:
            clis = random.sample(self._clients, 1)
        generator = util.run_script(
            SCRIPTS, self._map_async,
            'lq_get', clis, expireat=t_expireat, **self._params)

        failed_candidates = []
        winner = (None, None)
        for cclient, ch_k in generator:
            if isinstance(ch_k, Exception):
                failed_candidates.append((cclient, ch_k))
            else:
                winner = (cclient, ch_k)
                break
        failed_clients = (
            cclient for cclient, ch_k in chain(generator, failed_candidates))
        list(util.run_script(
            SCRIPTS, self._map_async,
            'lq_unlock', failed_clients,
            h_k=ch_k, **(self._params)))
        return winner

    def _acquire_lock_majority(self, client, h_k, t_start, t_expireat):
        """We've gotten and locked an item on a single redis instance.
        Attempt to get the lock on all remaining instances, and
        handle all scenarios where we fail to acquire the lock.

        Return True if acquired majority of locks, False otherwise.
        """
        locks = util.run_script(
            SCRIPTS, self._map_async, 'lq_lock',
            [x for x in self._clients if x != client],
            h_k=h_k, expireat=t_expireat, **(self._params))
        locks = list(locks)
        locks.append((client, 1))
        if not self._verify_not_already_completed(locks, h_k):
            return False
        if not self._have_majority(locks, h_k):
            return False
        if not util.lock_still_valid(
                t_expireat, self._clock_drift, self._polling_interval):
            return False
        return True

    def _verify_not_already_completed(self, locks, h_k):
        """If any Redis server reported that the key, `h_k`, was completed,
        return False and update all servers that don't know this fact.
        """
        completed = ["%s" % l == "already completed" for _, l in locks]
        if any(completed):
            outdated_clients = [
                cli for (cli, _), marked_done in zip(locks, completed)
                if not marked_done]
            list(util.run_script(
                SCRIPTS, self._map_async,
                'lq_consume',
                clients=outdated_clients,
                h_k=h_k, **(self._params)))
            return False
        return True

    def _have_majority(self, locks, h_k):
        """Evaluate whether the number of obtained is > half the number of
        redis servers.  If didn't get majority, unlock the locks we got.

        `locks` - a list of (client, have_lock) pairs.
            client is one of the redis clients
            have_lock may be 0, 1 or an Exception
        """
        cnt = sum(x[1] == 1 for x in locks if not isinstance(x, Exception))
        if cnt < (self._n_servers // 2 + 1):
            log.warn("Could not get majority of locks for item.", extra=dict(
                h_k=h_k))
            list(util.run_script(
                SCRIPTS, self._map_async,
                'lq_unlock', [cli for cli, lock in locks if lock == 1],
                h_k=h_k, **(self._params)))
            return False
        return True