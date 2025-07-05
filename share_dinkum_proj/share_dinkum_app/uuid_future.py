import time
import os
from uuid import UUID


# # (My version is much simpler, but using the official one is better for compatibility)
# def uuid7():
#     """A basic UUID7 implmentation. The leading 48 bits are the timestamp from unix epoch, so the values will increase monotonically over time"""
#     ms = time.time_ns() // 1000000
#     rand_a = int.from_bytes(os.urandom(2), byteorder='big')
#     rand_b = int.from_bytes(os.urandom(8), byteorder='big')
#     version = 0x07
#     var = 2
#     rand_a &= 0xfff
#     rand_b &= 0x3fffffffffffffff
#     uuid_bytes = ms.to_bytes(6, byteorder='big')
#     uuid_bytes += ((version<<12)+rand_a).to_bytes(2, byteorder='big')
#     uuid_bytes += ((var<<62)+rand_b).to_bytes(8, byteorder='big')
#     return UUID(bytes=uuid_bytes)



# Following code excerpts is from the standard library uuid module


# RFC 4122 variant bits and version bits to activate on a UUID integral value.
_RFC_4122_VERSION_7_FLAGS = ((7 << 76) | (0x8000 << 48))

_last_timestamp_v7 = None
_last_counter_v7 = 0  # 42-bit counter

def _uuid7_get_counter_and_tail():
    rand = int.from_bytes(os.urandom(10))
    # 42-bit counter with MSB set to 0
    counter = (rand >> 32) & 0x1ff_ffff_ffff
    # 32-bit random data
    tail = rand & 0xffff_ffff
    return counter, tail


def uuid7():
    """Generate a UUID from a Unix timestamp in milliseconds and random bits.

    UUIDv7 objects feature monotonicity within a millisecond.
    """
    # --- 48 ---   -- 4 --   --- 12 ---   -- 2 --   --- 30 ---   - 32 -
    # unix_ts_ms | version | counter_hi | variant | counter_lo | random
    #
    # 'counter = counter_hi | counter_lo' is a 42-bit counter constructed
    # with Method 1 of RFC 9562, §6.2, and its MSB is set to 0.
    #
    # 'random' is a 32-bit random value regenerated for every new UUID.
    #
    # If multiple UUIDs are generated within the same millisecond, the LSB
    # of 'counter' is incremented by 1. When overflowing, the timestamp is
    # advanced and the counter is reset to a random 42-bit integer with MSB
    # set to 0.

    global _last_timestamp_v7
    global _last_counter_v7

    nanoseconds = time.time_ns()
    timestamp_ms = nanoseconds // 1_000_000

    if _last_timestamp_v7 is None or timestamp_ms > _last_timestamp_v7:
        counter, tail = _uuid7_get_counter_and_tail()
    else:
        if timestamp_ms < _last_timestamp_v7:
            timestamp_ms = _last_timestamp_v7 + 1
        # advance the 42-bit counter
        counter = _last_counter_v7 + 1
        if counter > 0x3ff_ffff_ffff:
            # advance the 48-bit timestamp
            timestamp_ms += 1
            counter, tail = _uuid7_get_counter_and_tail()
        else:
            # 32-bit random data
            tail = int.from_bytes(os.urandom(4))

    unix_ts_ms = timestamp_ms & 0xffff_ffff_ffff
    counter_msbs = counter >> 30
    # keep 12 counter's MSBs and clear variant bits
    counter_hi = counter_msbs & 0x0fff
    # keep 30 counter's LSBs and clear version bits
    counter_lo = counter & 0x3fff_ffff
    # ensure that the tail is always a 32-bit integer (by construction,
    # it is already the case, but future interfaces may allow the user
    # to specify the random tail)
    tail &= 0xffff_ffff

    int_uuid_7 = unix_ts_ms << 80
    int_uuid_7 |= counter_hi << 64
    int_uuid_7 |= counter_lo << 32
    int_uuid_7 |= tail
    # by construction, the variant and version bits are already cleared
    int_uuid_7 |= _RFC_4122_VERSION_7_FLAGS

    bytes = int_uuid_7.to_bytes(16, byteorder='big')
    res = UUID(bytes=bytes)

    # defer global update until all computations are done
    _last_timestamp_v7 = timestamp_ms
    _last_counter_v7 = counter
    return res