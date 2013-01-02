import os

from ptrace  import *
from syscall import *

TRACE_PTRACE  = 0
TRACE_SECCOMP = 1

WALL = 0x40000000

def run(opt, args):
    pid = os.fork()
    
    # child
    if pid == 0:
        ptrace_traceme()
        # install seccomp bfp
        if opt == TRACE_SECCOMP:
            import seccomp
            seccomp.install_seccomp()
        # run tracee
        os.execvp(args[0], args)
        print "Failed to execute: %s" % " ".join(args)
        exit(1)
    # parent
    else:
        (pid, status) = os.wait()

        # following child
        set_ptrace_flags(pid)

        # interpose either next seccomp event or syscall
        if opt == TRACE_SECCOMP:
            ptrace_cont(pid)
        else:
            ptrace_syscall(pid)
        
    return pid

def set_ptrace_flags(pid):
    # set to follow children
    ptrace(PTRACE_SETOPTIONS, pid, 0,
           PTRACE_O_TRACESYSGOOD    # SIGTRAP|0x80 if syscall call traps
           | PTRACE_O_TRACEFORK     # PTRACE_EVENT_FORK
           | PTRACE_O_TRACEVFORK    # PTRACE_EVENT_VFORK
           | PTRACE_O_TRACECLONE    # PTRACE_EVENT_CLONE
           | PTRACE_O_TRACEEXEC     # PTRACE_EVENT_EXEC
           | PTRACE_O_TRACESECCOMP) # PTRACE_EVENT_SECCOMP

PS_RUNNING = 1
PS_IGNSTOP = 2

class Process(object):
    def __init__(self, pid, sigstop=False):
        self.robuf = None
        self.gen   = 0
        self.pid   = pid
        self.sc    = None
        self.regs  = (-1, None)
        self.state = PS_RUNNING

        self.set_arg_robuf()
        
        if sigstop:
            self.set_ptrace_flags()
        
    def set_arg_robuf(self):
        self.robuf = None
        
        # iterate maps and find the elf header
        self.img = os.readlink("/proc/%s/exe" % self.pid)
        for l in open("/proc/%s/maps" % self.pid):
            l = l.rstrip()
            if l.endswith(self.img) and "r-xp" in l:
                self.robuf = int(l.split("-")[0], 16)
                break

        # verify robuf is properly loaded
        if not self.robuf:
            dbg.warn("Could not find the elf header (readonly memory), use %rsp")

        # check if writable
        if self.robuf:
            word = 0xdeadbeef
            self.poke(self.robuf, word)
            peek = byte2word(self.peek(self.robuf))
            if peek != word:
                dbg.warn("0x%x is not writable (read=%x, but write=%x)" \
                             % (self.robuf, peek, word))

            # check if process_vm_readv works
            readv = ptrace_readmem(self.pid, self.robuf, 8)
            if byte2word(readv) == word:
                # use readv instead of ptrace peek/poke
                self.read_str    = self.read_str_readv
                self.read_bytes  = self.read_bytes_readv
                # XXX.
                # self.write_bytes = self.write_bytes_writev
                pass
            else:
                dbg.warn("process_vm_readv(addr=%x)=%s, word=%x)" \
                             % (self.robuf, readv.value, word))

    def get_arg_robuf(self, arg):
        if self.robuf:
            return self.robuf + arg * MAX_PATH
        return self.getreg("rsp") - MAX_PATH * arg

    def set_ptrace_flags(self):
        set_ptrace_flags(self.pid)
        self.state = PS_IGNSTOP

    def set_ptrace_flags_done(self):
        self.state = PS_RUNNING

    def is_exiting(self):
        return self.sc is None or self.sc.exiting
    
    def syscall(self):
        # inc generation
        self.gen += 1

        # new syscall
        if self.is_exiting():
            self.sc = Syscall(self)
        else:
            self.sc.update()

        return self.sc

    def getregs(self):
        if self.regs[0] != self.gen:
            regs = ptrace_getregs(self.pid)
            self.regs = (self.gen, regs)
        return self.regs[1]

    def getreg(self, regname):
        regs = self.getregs()
        return getattr(regs, regname)

    def setregs(self, regs):
        return ptrace_setregs(self.pid, regs)

    def setreg(self, regname, value):
        regs = self.getregs()
        setattr(regs, regname, value)
        ptrace_setregs(self.pid, regs)

    def peek(self, addr):
        return ptrace_peek(self.pid, addr)

    def poke(self, addr, word):
        return ptrace_poke(self.pid, addr, word)

    def read_bytes_readv(self, ptr, size):
        return ptrace_readmem(self.pid, ptr, size)
    
    def read_bytes(self, ptr, size):
        data = b''
        WORD = 8
        offset = ptr % WORD
        if offset:
            # read word
            ptr -= offset
            blob = self.peek(ptr)

            # read some bytes from the word
            subsize = min(WORD - offset, size)
            data = blob[offset:offset+subsize]

            # move cursor
            size -= subsize
            ptr += WORD
            
        while size:
            # read word
            blob = self.peek(ptr)

            # read bytes from the word
            if size < WORD:
                data += blob[:size]
                break
            data += blob

            # move cursor
            size -= WORD
            ptr += WORD
            
        return data

    def read_str(self, ptr, limit=1024):
        rtn = []
        WORD = 8
        while len(rtn) < limit:
            blob = self.peek(ptr)
            null = blob.find(b'\0')
            # done
            if null != -1:
                rtn.extend(blob[:null])
                break
            rtn.extend(blob)
            ptr += WORD
        return ''.join(rtn)

    def read_str_readv(self, ptr, limit=1024):
        rtn = []
        LEN = 256
        while len(rtn) < limit:
            blob = ptrace_readmem(self.pid, ptr, LEN)
            null = blob.find(b'\0')
            # done
            if null != -1:
                rtn.extend(blob[:null])
                break
            rtn.extend(blob)
            ptr += WORD
        return ''.join(rtn)

    def write_bytes_writev(self, ptr, blob):
        ptrace_writemem(self.pid, ptr, blob)

    def write_bytes(self, ptr, blob):
        # off
        # [..bb]...[ee..]
        #    ^        ^
        #    +-- ptr  |
        # [byte]      |
        #             rear
        #             
        WORD = 8

        # adjust front bytes
        off = ptr % WORD
        if off:
            ptr  = ptr - off
            byte = self.peek(ptr)
            blob = byte[:off] + blob

        # adjust rear bytes
        rear = ptr + len(blob)
        off = rear % WORD
        if off:
            byte = self.peek(rear - off)
            blob = blob + byte[off:]

        assert len(blob) % WORD == 0

        # write
        for i in range(0, len(blob), WORD):
            self.poke(ptr + i, byte2word(blob[i:i+WORD]))

    def write_str(self, ptr, blob):
        self.write_bytes(ptr, blob + '\x00')