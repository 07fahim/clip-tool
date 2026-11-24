"""The two shared limits on heavy work: one download at a time, one CPU-heavy step at a time.

A cut spends part of its time waiting on the network and part on the CPU (Whisper and the render). Holding the two
separately lets the next clip download while the last one renders, and still never runs two renders or two Whisper
runs at once, which would each take twice as long.
"""
import threading

DOWNLOAD_LOCK = threading.Lock()
COMPUTE_LOCK = threading.Lock()
