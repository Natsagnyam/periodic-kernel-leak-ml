#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/uaccess.h>
#include <linux/slab.h>
#include <linux/ktime.h>
#include <linux/string.h>
#include <linux/types.h>

#define RING_SHIFT       12U
#define RING_SIZE        (1U << RING_SHIFT)
#define RING_MASK        (RING_SIZE - 1U)
#define FAST_LOOP_ITERS  8U
#define LEAK_BLOCK_BYTES 64U

struct abb_sample {
    u64 ts_ns;
    u32 bytes;
    u32 iter;
};

struct abb_prod_ctrl {
    u8 pad_top[124];
    volatile u32 tail;
    u8 pad_bot[124];
} __attribute__((aligned(64)));

struct abb_cons_ctrl {
    u8 pad_top[124];
    volatile u32 head;
    u8 pad_bot[124];
} __attribute__((aligned(64)));

static struct abb_prod_ctrl g_prod;
static struct abb_cons_ctrl g_cons;
static struct abb_sample g_ring[RING_SIZE];
static u64 g_leaked_total;

/* Fast-loop macro performing unfreed periodic allocation */
#define ABB_FAST_LOOP_LEAK_MACRO(sz, out_p, ok) do {          \
    (out_p) = kmalloc((sz), GFP_ATOMIC);                        \
    if (likely((out_p) != NULL)) {                              \
        memset((out_p), 0x3C, (sz));                            \
        g_leaked_total += (u64)(sz);                            \
        (ok) = 1U;                                              \
    } else {                                                    \
        (ok) = 0U;                                              \
    }                                                           \
} while (0)

static inline void abb_ring_push(u64 ts_ns, u32 bytes, u32 iter)
{
    u32 t = g_prod.tail & RING_MASK;
    g_ring[t].ts_ns = ts_ns;
    g_ring[t].bytes = bytes;
    g_ring[t].iter = iter;
    g_prod.tail = (t + 1U) & RING_MASK;
}

static ssize_t abb_leak_read(struct file *file, char __user *ubuf,
                             size_t count, loff_t *ppos)
{
    void *ptr = NULL;
    u32 i;
    u32 ok = 0U;
    u64 base_ns = ktime_get_ns();
    u32 h;
    u32 t;

    (void)file;
    (void)ppos;

    if (count < sizeof(struct abb_sample)) {
        return -EINVAL;
    }

    for (i = 0U; i < FAST_LOOP_ITERS; ++i) {
        ABB_FAST_LOOP_LEAK_MACRO(LEAK_BLOCK_BYTES, ptr, ok);
        if (ok) {
            abb_ring_push(base_ns + ((u64)i * 250ULL), LEAK_BLOCK_BYTES, i);
        }
    }

    h = g_cons.head & RING_MASK;
    t = g_prod.tail & RING_MASK;

    if (h == t) {
        return 0;
    }

    if (copy_to_user(ubuf, &g_ring[h], sizeof(struct abb_sample))) {
        return -EFAULT;
    }

    g_cons.head = (h + 1U) & RING_MASK;
    return (ssize_t)sizeof(struct abb_sample);
}

static const struct file_operations abb_leak_fops = {
    .owner = THIS_MODULE,
    .read  = abb_leak_read,
};

static struct miscdevice abb_leak_misc = {
    .minor = MISC_DYNAMIC_MINOR,
    .name  = "abb_leak",
    .fops  = &abb_leak_fops,
    .mode  = 0444,
};

static int __init abb_leak_init(void)
{
    int ret = misc_register(&abb_leak_misc);
    if (ret) {
        pr_err("abb_leak_mod: misc_register failed (%d)\n", ret);
        return ret;
    }
    pr_info("abb_leak_mod: loaded (/dev/abb_leak)\n");
    return 0;
}

static void __exit abb_leak_exit(void)
{
    misc_deregister(&abb_leak_misc);
    pr_info("abb_leak_mod: unloaded (total_leaked_bytes=%llu)\n",
            (unsigned long long)g_leaked_total);
}

module_init(abb_leak_init);
module_exit(abb_leak_exit);
MODULE_LICENSE("GPL");
MODULE_AUTHOR("Natska Kernel Lab");
MODULE_DESCRIPTION("ABB fast-loop periodic memory leak test module");
