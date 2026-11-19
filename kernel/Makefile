obj-m += abb_leak_mod.o

KDIR ?= /lib/modules/$(shell uname -r)/build
MOD_SRC := /home/natska/abb-kernel-memory-viewver/kernel

all:
	$(MAKE) -C $(KDIR) M=$(MOD_SRC) modules

clean:
	$(MAKE) -C $(KDIR) M=$(MOD_SRC) clean
