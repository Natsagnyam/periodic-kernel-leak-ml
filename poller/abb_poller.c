#include <stdio.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <time.h>

#define POLL_STEP_NS 2000000ULL /* 2 ms periodic poll */

struct abb_sample {
    uint64_t ts_ns;
    uint32_t bytes;
    uint32_t iter;
};

static inline uint64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ((uint64_t)ts.tv_sec * 1000000000ULL) + (uint64_t)ts.tv_nsec;
}

int main(void)
{
    static struct abb_sample pkt;
    int fd;
    uint64_t target_ns;

    fd = open("/dev/abb_leak", O_RDONLY);
    if (fd < 0) {
        perror("open /dev/abb_leak");
        return 1;
    }

    setvbuf(stdout, NULL, _IOLBF, 0);
    target_ns = mono_ns();

    for (;;) {
        uint64_t now = mono_ns();
        if (now >= target_ns) {
            ssize_t n = read(fd, &pkt, sizeof(pkt));
            if (n == (ssize_t)sizeof(pkt)) {
                printf("%llu,%u,%u\n",
                       (unsigned long long)pkt.ts_ns,
                       pkt.bytes,
                       pkt.iter);
            }
            target_ns = now + POLL_STEP_NS;
        }
    }

    close(fd);
    return 0;
}
