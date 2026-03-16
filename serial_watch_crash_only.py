import os
import time
import select
import termios

PORT = "/dev/tty.usbmodem2101"
DURATION_SECONDS = 540
TOKENS = (
    "Traceback",
    "MemoryError",
    "Events in 24h",
    "sync_and_refresh",
)


def configure_port(fd):
    try:
        attrs = termios.tcgetattr(fd)
        attrs[4] = termios.B115200
        attrs[5] = termios.B115200
        attrs[3] = attrs[3] & ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        pass


def main():
    start = time.time()
    matched = 0
    fd = None
    buf = b""

    while time.time() - start < DURATION_SECONDS:
        if fd is None:
            try:
                fd = os.open(PORT, os.O_RDONLY | os.O_NONBLOCK)
                configure_port(fd)
                buf = b""
                print("t=%4ds serial connected" % int(time.time() - start))
            except OSError:
                time.sleep(0.5)
                continue

        ready, _, _ = select.select([fd], [], [], 1.0)
        if not ready:
            continue
        try:
            data = os.read(fd, 4096)
        except OSError:
            try:
                os.close(fd)
            except Exception:
                pass
            fd = None
            print("t=%4ds serial disconnected" % int(time.time() - start))
            continue
        except BlockingIOError:
            continue
        if not data:
            continue

        buf += data
        while b"\n" in buf:
            raw_line, buf = buf.split(b"\n", 1)
            line = raw_line.decode("utf-8", "ignore").rstrip("\r")
            if not line:
                continue
            if any(token in line for token in TOKENS):
                matched += 1
                print("t=%4ds %s" % (int(time.time() - start), line))

    if fd is not None:
        os.close(fd)
    print("WATCH_DONE seconds=%d matched=%d" % (int(time.time() - start), matched))


if __name__ == "__main__":
    main()
