#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/uaccess.h>
#include <linux/ktime.h>
#include <linux/mm.h>
#include <linux/slab.h>
#include <linux/string.h>
#include <linux/types.h>

#define RING_SHIFT       11U
#define RING_SIZE        (1U << RING_SHIFT)
#define RING_MASK        (RING_SIZE - 1U)
#define BURST_PER_READ   64U
#define TEST_ALLOC_BYTES 4096U
#define MAX_TEST_ALLOCS  256U

#define add(a, b, c) ((c) = (a) + (b))
#define sub(a, b, c) ((c) = (a) - (b))


struct ram_sample {
    s64 ts_ns;
    u64 used_ram_kb;
    u64 total_ram_kb;
    u32 loop_running;
    u32 panic_alarm;
};

struct prod_idx {
    u8 pad_top[124];
    volatile u32 tail;
    u8 pad_bot[124];
} __attribute__((aligned(64)));

struct cons_idx {
    u8 pad_top[124];
    volatile u32 head;
    u8 pad_bot[124];
} __attribute__((aligned(64)));

static struct prod_idx g_prod;
static struct cons_idx g_cons;
static struct ram_sample g_ring[RING_SIZE];

static DEFINE_SPINLOCK(g_lock);
static volatile u32 g_loop_running;
static volatile u32 g_panic_alarm;
static u64 g_acc_a = 1ULL;
static u64 g_acc_b = 1ULL;
static u64 g_acc_c = 0ULL;
static u64 g_ticks = 0ULL;

static void *g_test_ptrs[MAX_TEST_ALLOCS];
static u32 g_test_alloc_count;

static void free_test_allocs_locked(void)
{
    u32 i;
    for (i = 0U; i < g_test_alloc_count; ++i) {
        kfree(g_test_ptrs[i]);
        g_test_ptrs[i] = NULL;
    }
    g_test_alloc_count = 0U;
}

static u64 read_used_ram_kb(u64 *out_total_kb)
{
    struct sysinfo si;
    u64 total_kb, free_kb, used_kb;

    si_meminfo(&si);
    total_kb = ((u64)si.totalram * (u64)si.mem_unit) >> 10;
    free_kb  = (((u64)si.freeram + (u64)si.bufferram) * (u64)si.mem_unit) >> 10;
    used_kb  = (total_kb > free_kb) ? (total_kb - free_kb) : 0ULL;

    if (out_total_kb) {
        *out_total_kb = total_kb;
    }
    return used_kb;
}

static void ring_push(s64 ts_ns, u64 used_kb, u64 total_kb,
                      u32 running, u32 panic)
{
    u32 t = g_prod.tail & RING_MASK;
    g_ring[t].ts_ns = ts_ns;
    g_ring[t].used_ram_kb = used_kb;
    g_ring[t].total_ram_kb = total_kb;
    g_ring[t].loop_running = running;
    g_ring[t].panic_alarm = panic;
    g_prod.tail = (t + 1U) & RING_MASK;
}

static void run_fast_loop(u32 n)
{
    unsigned long flags;
    u32 i;
    u64 a, b, c;
    u64 total_kb = 0ULL;
    u64 used_kb = 0ULL;
    s64 now_ns = (s64)ktime_get_ns();

    spin_lock_irqsave(&g_lock, flags);

    if (g_loop_running && !g_panic_alarm) {
        a = g_acc_a;
        b = g_acc_b;
        c = g_acc_c;
        for (i = 0U; i < n; ++i) {
            add(a, b, c);
            add(a, b, c);
            add(a, b, c);
            add(a, b, c);
            add(a, b, c);
            add(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
            sub(a, b, c);
                        
            a = b;
            b = c;

            if (g_test_alloc_count < MAX_TEST_ALLOCS<<16) {
                void *p = kmalloc(TEST_ALLOC_BYTES, GFP_ATOMIC);
                if (p) {
                    g_test_ptrs[g_test_alloc_count++] = p;
                }
            }
        }
        g_acc_a = a;
        g_acc_b = b;
        g_acc_c = c;
        g_ticks += (u64)n;
    }

    used_kb = read_used_ram_kb(&total_kb);
    ring_push(now_ns, used_kb, total_kb, g_loop_running, g_panic_alarm);

    spin_unlock_irqrestore(&g_lock, flags);
}

static ssize_t gauge_read(struct file *file, char __user *ubuf,
                          size_t count, loff_t *ppos)
{
    u32 h, t;

    (void)file;
    (void)ppos;

    if (count < sizeof(struct ram_sample)) {
        return -EINVAL;
    }

    run_fast_loop(BURST_PER_READ);

    h = g_cons.head & RING_MASK;
    t = g_prod.tail & RING_MASK;
    if (h == t) {
        return 0;
    }

    if (copy_to_user(ubuf, &g_ring[h], sizeof(struct ram_sample))) {
        return -EFAULT;
    }

    g_cons.head = (h + 1U) & RING_MASK;
    return (ssize_t)sizeof(struct ram_sample);
}

static ssize_t gauge_write(struct file *file, const char __user *ubuf,
                           size_t count, loff_t *ppos)
{
    char cmd[32];
    size_t len = min(count, sizeof(cmd) - 1U);
    unsigned long flags;

    (void)file;
    (void)ppos;

    if (len == 0U) {
        return 0;
    }
    if (copy_from_user(cmd, ubuf, len)) {
        return -EFAULT;
    }
    cmd[len] = '\0';

    spin_lock_irqsave(&g_lock, flags);

    if (sysfs_streq(cmd, "START")) {
        g_loop_running = 1U;
        g_panic_alarm = 0U;
    } else if (sysfs_streq(cmd, "STOP")) {
        g_loop_running = 0U;
    } else if (sysfs_streq(cmd, "RESET")) {
        g_loop_running = 0U;
        g_panic_alarm = 0U;
        g_acc_a = 1ULL;
        g_acc_b = 1ULL;
        g_acc_c = 0ULL;
        g_ticks = 0ULL;
        free_test_allocs_locked();
    }

    spin_unlock_irqrestore(&g_lock, flags);
    return (ssize_t)count;
}

static const struct file_operations gauge_fops = {
    .owner = THIS_MODULE,
    .read  = gauge_read,
    .write = gauge_write,
};

static struct miscdevice gauge_misc = {
    .minor = MISC_DYNAMIC_MINOR,
    .name  = "gauge_macro",
    .fops  = &gauge_fops,
    .mode  = 0666,
};

static int __init gauge_macro_init(void)
{
    int ret = misc_register(&gauge_misc);
    if (ret) {
        return ret;
    }
    pr_info("gauge_macro_mod: loaded (/dev/gauge_macro, kmalloc test active)\n");
    return 0;
}

static void __exit gauge_macro_exit(void)
{
    unsigned long flags;
    spin_lock_irqsave(&g_lock, flags);
    g_loop_running = 0U;
    free_test_allocs_locked();
    spin_unlock_irqrestore(&g_lock, flags);
    misc_deregister(&gauge_misc);
    pr_info("gauge_macro_mod: unloaded and test kmallocs freed\n");
}

module_init(gauge_macro_init);
module_exit(gauge_macro_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("Natska Kernel Lab");
MODULE_DESCRIPTION("Fast-loop add() macro + bounded kmalloc RAM test module");
