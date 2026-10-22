"""Transport-independent service errors."""


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


class Overloaded(Exception):
    pass


class LostLease(Exception):
    pass
