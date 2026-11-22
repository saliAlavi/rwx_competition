"""Run main.py as a child and report the CPU it and its descendants used (stderr)."""
import resource, subprocess, sys, time
t = time.time()
rc = subprocess.call([sys.executable, "/app/main.py"])
u = resource.getrusage(resource.RUSAGE_CHILDREN)
print(f"[cpu_wrap] rc={rc} wall={time.time()-t:.2f}s cpu={u.ru_utime+u.ru_stime:.2f}s "
      f"(user {u.ru_utime:.2f} sys {u.ru_stime:.2f}) maxrss={u.ru_maxrss/1024:.0f}MB", file=sys.stderr)
sys.exit(rc)
