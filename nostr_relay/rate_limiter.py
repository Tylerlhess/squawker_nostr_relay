import time
import operator
import logging
import collections
import importlib


class BaseRateLimiter:
    def __init__(self, options=None):
        self.log = logging.getLogger('nostr_relay.limiter')

    def cleanup(self):
        pass


class RateLimiter(BaseRateLimiter):
    """
    A configurable rate limiter
    
    The options dict looks like:
    {
        "global": {
            "EVENT": "10/s,100/hour,40/min"
        },
        "ip": {
            "REQ": "10/minute"
        }
    }
    """
    def __init__(self, options=None):
        super().__init__(options)
        self.log = logging.getLogger('nostr_relay.limiter')
        self.global_limits, self.ip_limits = self.parse_options(options or {})
        self.recent_commands = collections.defaultdict(lambda: collections.defaultdict(collections.deque))

    def parse_options(self, options):
        global_limits = {}
        ip_limits = {}
        for key, value in options.get('global', {}).items():
            global_limits[key] = self.parse_option(value)
        for key, value in options.get('ip', {}).items():
            ip_limits[key] = self.parse_option(value)
        self.log.debug("Parsed rate limits: global:%s ip:%s", global_limits, ip_limits)
        self.log.info("Rate limiter enabled")
        return global_limits, ip_limits

    def parse_option(self, option):
        rules = []
        for rule in option.split(','):
            if not rule:
                continue
            try:
                freq, resolution = rule.split('/')
            except ValueError:
                continue
            resolution = resolution.lower()
            if resolution in ('s', 'second', 'sec'):
                resolution = 1.0
            elif resolution in ('m', 'minute', 'min'):
                resolution = 60.0
            elif resolution in ('h', 'hour', 'hr'):
                resolution = 3600
            else:
                raise ValueError(resolution)
            rules.append((resolution, float(freq)))
        rules.sort(reverse=True)
        return rules

    def evaluate_rules(self, rules, timestamps):
        now = time.time()
        if timestamps:
            last_time = timestamps[-1]
            for resolution, freq in rules:
                count = 0
                for ts in reversed(timestamps):
                    if (now - ts) < resolution:
                        count += 1
                    if count == freq:
                        self.log.debug("%d/%d", freq, resolution)
                        return True
            if (now - timestamps[-1]) > max(rules)[0]:
                timestamps.clear()
        return False

    def is_limited(self, ip_address, message):
        command = message[0]
        self.log.debug("Checking limits for %s %s", command, ip_address)
        if command in self.global_limits:
            if self.evaluate_rules(self.global_limits[command], self.recent_commands['global'][command]):
                self.log.warning("Rate limiting globally for %s", command)
                return True
            self.recent_commands['global'][command].append(time.time())
        if command in self.ip_limits:
            if self.evaluate_rules(self.ip_limits[command], self.recent_commands[ip_address][command]):
                self.log.warning("Rate limiting %s for %s", ip_address, command)
                return True
            self.recent_commands[ip_address][command].append(time.time())
        return False

    def cleanup(self):
        max_resolution = 0
        if not self.ip_limits:
            return
        for rules in self.ip_limits.values():
            rule_res = max(rules)[0]
            max_resolution = max(rule_res, max_resolution)

        now = time.time()
        to_del = []
        for ip, commands in self.recent_commands.items():
            if ip == 'global':
                continue

            cleared = []
            for cmd, ts in commands.items():
                if (not ts) or (now - ts[-1]) > max_resolution:
                    ts.clear()
                    cleared.append(cmd)
            if len(cleared) == len(commands):
                to_del.append(ip)
        for k in to_del:
            try:
                del self.recent_commands[k]
            except KeyError:
                pass


class NullRateLimiter(BaseRateLimiter):
    """
    A rate limiter that does nothing
    """
    def is_limited(self, ip_address, message):
        return False


def get_rate_limiter(options):
    """
    Return a rate limiter instance.
    If options["rate_limiter_class"] is set, will import and use that implementation.
    Otherwise, we'll use RateLimiter

    If options["rate_limits"] is not set, return NullRateLimiter
    """
    if 'rate_limits' in options:
        classpath = options.get('rate_limiter_class', 'nostr_relay.rate_limiter:RateLimiter')
        modulename, classname = classpath.split(':', 1)
        module = importlib.import_module(modulename)
        classobj = getattr(module, classname)
    else:
        classobj = NullRateLimiter
    return classobj(options.get('rate_limits', {}))
