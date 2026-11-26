#!/usr/bin/env bash
# Low-latency Linux host check / tuning for the exchange and the CPU contestant.
#
#   tools/linux_tune.sh [--iface eth1] [--trading-cores 2-5] [--housekeeping 0-1]          # report only
#   sudo tools/linux_tune.sh --apply [--iface eth1] [--trading-cores 2-5] [--housekeeping 0-1]
#
# Default is a dry run: it reports the current state and the command it WOULD run for each item.
# --apply runs only the runtime changes (lost on reboot). Boot parameters (CPU isolation) are
# never changed: the script prints the kernel command line to add to your bootloader.
#
# What it covers, in order of how much it usually matters for tail latency:
#   1. CPU isolation   isolcpus / nohz_full / rcu_nocbs keep the scheduler, timer ticks and RCU
#                      callbacks off the trading cores (boot parameters; reported only)
#   2. NIC interrupts  irqbalance off; NIC IRQs pinned to housekeeping cores, never trading cores
#   3. CPU frequency   performance governor, no deep C-states (wake-up from C6 costs tens of us)
#   4. Busy polling    net.core.busy_read / busy_poll so sockets spin on the NIC queue
#   5. NIC coalescing  interrupt moderation off, GRO/LRO off (they batch packets = add delay)
#   6. Memory          transparent hugepages to madvise (no khugepaged stalls), big socket buffers
#   7. Real-time       SCHED_FIFO is only safe on isolated cores: on a shared core a spinning
#                      real-time thread starves the kernel's network softirqs (we measured p99
#                      going from 3 us to 65 us in a VM). The script says which case you are in.
set -uo pipefail

APPLY=0; IFACE=""; TRADING=""; HOUSE="0"
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --iface) IFACE="$2"; shift ;;
    --trading-cores) TRADING="$2"; shift ;;
    --housekeeping) HOUSE="$2"; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
  shift
done

[ "$(uname -s)" = "Linux" ] || { echo "Linux only."; exit 1; }
if [ $APPLY -eq 1 ] && [ "$(id -u)" -ne 0 ]; then echo "--apply needs root (sudo)."; exit 1; fi

OK="[ok]  "; WARN="[fix] "; INFO="[info]"
do_or_say() {   # do_or_say "description" command...
  local desc="$1"; shift
  if [ $APPLY -eq 1 ]; then
    if "$@" >/dev/null 2>&1; then echo "        applied: $desc"; else echo "        FAILED:  $desc ($*)"; fi
  else
    echo "        would run: $*"
  fi
}
sysctl_get() { sysctl -n "$1" 2>/dev/null || echo "?"; }

NCPU=$(nproc)
CMDLINE=$(cat /proc/cmdline)
echo "host: $(uname -r), $NCPU CPUs, $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2 | sed 's/^ //')"
[ -n "$TRADING" ] || TRADING="2-$(( NCPU > 6 ? 5 : NCPU - 1 ))"
echo "trading cores: $TRADING   housekeeping cores: $HOUSE   NIC: ${IFACE:-<none given>}"
echo

echo "1. CPU isolation (boot parameters)"
ISOLATED=0
for p in isolcpus nohz_full rcu_nocbs; do
  if echo "$CMDLINE" | grep -qw "$p=[^ ]*"; then
    echo "$OK $p is set: $(echo "$CMDLINE" | grep -o "$p=[^ ]*")"; ISOLATED=1
  else
    echo "$WARN $p not set"
  fi
done
if [ $ISOLATED -eq 0 ]; then
  ARCH=$(uname -m)
  EXTRA="processor.max_cstate=1 intel_idle.max_cstate=0"
  [ "$ARCH" = "x86_64" ] || EXTRA="cpuidle.off=1"
  echo "        add to the kernel command line (e.g. GRUB_CMDLINE_LINUX in /etc/default/grub), then"
  echo "        update-grub && reboot:"
  echo "          isolcpus=$TRADING nohz_full=$TRADING rcu_nocbs=$TRADING $EXTRA"
fi
echo

echo "2. NIC interrupts"
if pgrep -x irqbalance >/dev/null 2>&1; then
  echo "$WARN irqbalance is running (it moves NIC interrupts onto trading cores)"
  if command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then do_or_say "stop irqbalance" systemctl stop irqbalance
  else do_or_say "stop irqbalance" pkill -x irqbalance; fi
else
  echo "$OK irqbalance not running"
fi
if [ -n "$IFACE" ]; then
  IRQS=$(grep -E "$IFACE" /proc/interrupts | awk -F: '{print $1}' | tr -d ' ')
  [ -z "$IRQS" ] && echo "$INFO no IRQs found for $IFACE in /proc/interrupts"
  for irq in $IRQS; do
    cur=$(cat /proc/irq/$irq/smp_affinity_list 2>/dev/null)
    if [ "$cur" = "$HOUSE" ]; then echo "$OK IRQ $irq on cores $cur"
    else echo "$WARN IRQ $irq on cores $cur"; do_or_say "IRQ $irq -> $HOUSE" sh -c "echo $HOUSE > /proc/irq/$irq/smp_affinity_list"; fi
  done
else
  echo "$INFO pass --iface to check NIC interrupt placement"
fi
echo

echo "3. CPU frequency and idle states"
GOVS=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null | sort | uniq -c | tr '\n' ' ')
if [ -z "$GOVS" ]; then echo "$INFO no cpufreq interface (VM or fixed frequency)"
elif echo "$GOVS" | grep -qv performance; then
  echo "$WARN governors: $GOVS"
  do_or_say "performance governor" sh -c 'for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do echo performance > $g; done'
else echo "$OK all CPUs on performance"; fi
if ls /sys/devices/system/cpu/cpu0/cpuidle/state* >/dev/null 2>&1; then
  DEEP=$(for s in /sys/devices/system/cpu/cpu0/cpuidle/state*; do [ "$(cat $s/disable)" = "0" ] && [ "$(cat $s/latency)" -gt 10 ] && echo "$(cat $s/name)($(cat $s/latency)us)"; done | tr '\n' ' ')
  if [ -n "$DEEP" ]; then
    echo "$WARN deep idle states enabled on cpu0: $DEEP"
    do_or_say "disable idle states with exit latency > 10 us" sh -c 'for s in /sys/devices/system/cpu/cpu*/cpuidle/state*; do [ "$(cat $s/latency)" -gt 10 ] && echo 1 > $s/disable; done'
  else echo "$OK no deep idle states enabled"; fi
fi
echo

echo "4. Busy polling"
for k in net.core.busy_read net.core.busy_poll; do
  v=$(sysctl_get $k)
  if [ "$v" = "?" ]; then echo "$INFO $k unavailable"
  elif [ "$v" -ge 50 ] 2>/dev/null; then echo "$OK $k = $v"
  else echo "$WARN $k = $v"; do_or_say "$k=50" sysctl -w $k=50; fi
done
echo

echo "5. NIC offloads and interrupt coalescing"
if [ -n "$IFACE" ] && command -v ethtool >/dev/null; then
  ethtool -c "$IFACE" 2>/dev/null | grep -E "^(Adaptive|rx-usecs|tx-usecs):" | sed 's/^/        /'
  do_or_say "coalescing off" ethtool -C "$IFACE" adaptive-rx off adaptive-tx off rx-usecs 0 tx-usecs 0
  ethtool -k "$IFACE" 2>/dev/null | grep -E "^(generic-receive-offload|large-receive-offload):" | sed 's/^/        /'
  do_or_say "GRO/LRO off" ethtool -K "$IFACE" gro off lro off
else
  echo "$INFO pass --iface (and install ethtool) to check coalescing and offloads"
fi
echo

echo "6. Memory and socket buffers"
THP=$(cat /sys/kernel/mm/transparent_hugepage/enabled 2>/dev/null | grep -o '\[[a-z]*\]' | tr -d '[]')
if [ "$THP" = "always" ]; then echo "$WARN transparent hugepages = always"; do_or_say "THP madvise" sh -c 'echo madvise > /sys/kernel/mm/transparent_hugepage/enabled'
else echo "$OK transparent hugepages = ${THP:-unknown}"; fi
for k in net.core.rmem_max net.core.wmem_max; do
  v=$(sysctl_get $k)
  if [ "$v" != "?" ] && [ "$v" -ge 16777216 ] 2>/dev/null; then echo "$OK $k = $v"
  else echo "$WARN $k = $v"; do_or_say "$k=16MB" sysctl -w $k=16777216; fi
done
ML=$(ulimit -l)
if [ "$ML" = "unlimited" ]; then echo "$OK memlock limit unlimited (--mlock works)"
else echo "$WARN memlock limit $ML KB: --mlock may fail; raise it in /etc/security/limits.conf or run with ulimit -l unlimited"; fi
echo

echo "7. Real-time scheduling"
RT=$(sysctl_get kernel.sched_rt_runtime_us)
if [ $ISOLATED -eq 1 ]; then
  echo "$OK trading cores are isolated: --rt 50 is safe there"
  [ "$RT" != "-1" ] && echo "$INFO kernel.sched_rt_runtime_us=$RT throttles RT threads (95% default); -1 disables it (only with isolated cores)"
else
  echo "$WARN cores are NOT isolated: do not use --rt (a spinning SCHED_FIFO thread starves kernel softirqs on that core)"
fi
echo

echo "Suggested launch (exchange and contestant on different machines, each on an isolated core):"
FIRST=$(echo "$TRADING" | cut -d- -f1)
RTFLAG=""; [ $ISOLATED -eq 1 ] && RTFLAG=" --rt 50"
echo "  exchange: ./build/exchange run --messages FILE --host 0.0.0.0 --core $FIRST --busy-poll 50 --kernel-ts --mlock$RTFLAG --http 8080 ..."
echo "  trader:   ./build/trader --exchange <exchange-ip>:9000 --core $FIRST --busy-poll 50 --kernel-ts --mlock$RTFLAG ..."
[ $APPLY -eq 0 ] && echo && echo "(dry run: nothing was changed; re-run with sudo and --apply)"
