#!/usr/bin/python

from collections import defaultdict
import re
from copy import copy

class struct:
    def __init__(self,**kwargs):
        for k in kwargs:
            setattr(self,k,kwargs[k])
    def __eq__(self, other):
        return self.__dict__ == other.__dict__

meaning={
    'W': 'write',
    'D': 'discard',
    'R': 'read',
    'N': 'no-op',
    'F': '[fua]',
    'A': '[read-ahead]',
    'S': '[sync]',
    'M': '[metadata]',
    'E': '[secure]'
}

def decode_rwbs(rwbs):
    out=''
    if rwbs[0]=='F':
        out+='flushing '
        rwbs=rwbs[1:]
    for l in rwbs:
        out+=meaning[l]+' '
    return out

def parse(fn):
    f = file(fn)

    commbypid={} # Needed to handle exec correctly
    evs = []

    # First parse the events
    for line in f:
        if line[0]=='#':
            continue
        if line=='\n':
            continue
        m=re.match('(.*[^ ]) +([0-9]+)(?: \\[[0-9]*\\])? +([0-9]*\\.[0-9]*): +([^ ]*): +(.*)',line)
        if m:
            ev=struct()
            (ev.comm, ev.pid, ev.time, ev.event, args) = m.groups()
            ev.pid = int(ev.pid)
            ev.time = float(ev.time)
            # Add some nanoseconds for the case where the events happen too fast that they have the same time
 	    if len(evs) > 0:
		if ev.time <= evs[-1].time:
		   ev.time = evs[-1].time + 1e-9
            ev.args = struct()
            ev.stack = []
            if ev.event=='sched:sched_process_exec':
                if ev.pid in commbypid:
                    ev.oldcomm=commbypid[ev.pid]
                else:
                    ev.oldcomm='unknown(%d)'%ev.pid
            elif ev.pid in commbypid:
                if (commbypid[ev.pid]!=ev.comm and ev.pid!=0 and ev.comm!='kthreadd'): #DO NOT SUBMIT
                    fake_ev=struct()
                    fake_ev.comm=ev.comm
                    fake_ev.pid=ev.pid
                    fake_ev.time=ev.time
                    fake_ev.event='sched:sched_process_exec'
                    fake_ev.oldcomm=commbypid[ev.pid]
                    #print "WARNING: deduced exec call by %d from %s to %s at %f" % (ev.pid, fake_ev.oldcomm, ev.comm, ev.time)
                    evs.append(fake_ev)
            commbypid[ev.pid]=ev.comm
            prevkey=None
            for i in args.split(' '):
                m=re.match('([a-zA-Z_]*)=(.*)',i)
                if m:
                    setattr(ev.args, m.group(1), m.group(2))
                    prevkey=m.group(1)
                else:
                    if prevkey:
                        ev.args.__dict__[prevkey] += ' ' + i
                    else:
                        if not hasattr(ev.args, 'raw'):
                            ev.args.raw=[]
                        ev.args.raw.append(i)
            # keep track of children and nexts so that we dont't assume they're doing work
            if 'child_comm' in ev.args.__dict__:
                commbypid[int(ev.args.child_pid)] = ev.args.child_comm
            if 'next_comm' in ev.args.__dict__:
                commbypid[int(ev.args.next_pid)] = ev.args.next_comm
            evs.append(ev)
            continue
        m=re.search(' *[0-9a-f]* (.*) \\((.*)\\)',line)
        if m and evs:
            if m.group(1)!='[unknown]' or m.group(2)!='[unknown]':
                evs[-1].stack.append(struct(function=m.group(1),file=m.group(2)))
        else:
            print 'ERROR: Could not parse "%s"'%line

    # Now assemble the events into run blocks and links between them

    switchedin={}
    switchedout={}
    switchedoutstack={}
    inlinks=defaultdict(lambda:[])
    outlinks=defaultdict(lambda:[])
    runs=defaultdict(lambda:[])
    sleeps=defaultdict(lambda:[])
    runningstacks={}
    links=[]

    activedevs={}
    activebios={}
    bios=[]
    lastfinishforproc={}
    lastfinishondev={}

    starttime=evs[0].time
    endtime=evs[-1].time

    wokenbyinterrupt={}
    lastinterruption={}    

    special={}

    activenet={}
    justreceived={}

    lastlibc={}

    for ev in evs:
        if ev.event=='sched:sched_switch':
            oldp='%s(%s)'%(ev.args.prev_comm,ev.args.prev_pid)
            newp='%s(%s)'%(ev.args.next_comm,ev.args.next_pid)
            # Handle runs
            if oldp in switchedin:
                if switchedin[oldp]==-1:
                    print "ERROR: %s switched out without being in at %f" % (oldp, ev.time)
                    exit()
                else:
                    runs[oldp].append(struct(start=switchedin[oldp], end=ev.time))
                    if oldp in runningstacks:
                        runs[oldp][-1].stacks=runningstacks[oldp]
                    else:
                        runs[oldp][-1].stacks=[]
                    switchedin[oldp]=-1
            else:
                runs[oldp].append(struct(start=starttime, end=ev.time, stacks=[]))
                switchedin[oldp]=-1
            if oldp in runningstacks:
                del runningstacks[oldp]
            runningstacks[newp]=[]
            if sleeps[oldp]:
                runs[oldp][-1].prev=sleeps[oldp][-1]
            if newp in switchedin and switchedin[newp]!=-1:
                print "ERROR: %s switched in twice (%f and %f)"%(newp,ev.time,switchedin[newp])
            else:
                switchedin[newp]=ev.time
            # Handle sleeps (TODO: maybe unify with the above?)
            if newp in switchedout:
                if switchedout[newp]==-1:
                    print "ERROR: %s switched in without being out at %f" % (newp, ev.time)
                    exit()
                else:
                    sleeps[newp].append(struct(start=switchedout[newp], end=ev.time, stack=switchedoutstack[newp]))
                    switchedout[newp]=-1
            if runs[newp]:
                sleeps[newp][-1].prev=runs[newp][-1]
            if oldp in switchedout and switchedout[oldp]!=-1:
                print "ERROR: %s switched out twice (%f and %f)"%(oldp,ev.time,switchedout[oldp])
            else:
                switchedout[oldp]=ev.time
                if ev.stack and ev.stack[-1].function=='[unknown]' and ev.stack[-1].file=='[unknown]':
                    ev.stack=ev.stack[:-1]
                if oldp in lastlibc:
                    if ev.stack[-1].function==lastlibc[oldp].function and ev.stack[-1].file=='/lib/x86_64-linux-gnu/libc-2.19.so':
                        ev.stack += lastlibc[oldp].stack
                switchedoutstack[oldp]=ev.stack
            if newp in wokenbyinterrupt and sleeps[newp]:
                sleeps[newp][-1].interrupt = wokenbyinterrupt[newp]
                del wokenbyinterrupt[newp]
            if sleeps[newp] and newp in special:
                sleeps[newp][-1].special=special[newp]
                del special[newp]
            # Handle links
            if inlinks[newp]:
                for inlink in inlinks[newp]:
                    inlink.end=ev.time
            if inlinks[oldp]:
                for inlink in inlinks[oldp]:
                    inlink.targetrun=runs[oldp][-1]
                    if 'end' not in inlink.__dict__:
                        inlink.end=inlink.start
                    runs[oldp][-1].inlink=inlink
                inlinks[oldp]=[]
            if outlinks[oldp]:
                for outlink in outlinks[oldp]:
                    outlink.outtime=ev.time
                    outlink.sourcerun=runs[oldp][-1]
                outlinks[oldp]=[]
            is_interrupt=False
            is_bio=False
            for frame in ev.stack:
                if frame.function in ['retint_careful', 'wait_for_completion']:
                    is_interrupt=frame.function
                if frame.function == 'submit_bio_wait':
                    is_bio=True
            if is_interrupt and not is_bio:
                links.append(struct(source=oldp,target=oldp,start=ev.time,outtime=ev.time,sourcerun=runs[oldp][-1],horizontal=True))
                inlinks[oldp].append(links[-1])
                runs[oldp][-1].horizoutlink=links[-1]
                special[oldp]=is_interrupt
        elif ev.event in ['sched:sched_wakeup', 'sched:sched_process_fork']:
            source='%s(%s)'%(ev.comm,ev.pid)
            if ev.event=='sched:sched_wakeup':
                target='%s(%s)'%(ev.args.comm, ev.args.pid)
            else:
                target='%s(%s)'%(ev.args.child_comm, ev.args.child_pid)
            irq_wakeup={}
            for frame in ev.stack:
                if frame.function in ['do_IRQ','smp_reschedule_interrupt', 'bio_endio', 'apic_timer_interrupt', 'tcp_rcv_established', 'tcp_finish_connect', 'tcp_transmit_skb', 'udp_queue_rcv_skb']:
                    irq_wakeup[frame.function]=1
            if 'bio_endio' in irq_wakeup:
                if target in lastfinishforproc:
                    links.append(struct(source=lastfinishforproc[target].proc, sourcerun=lastfinishforproc[target], target=target, start=ev.time, outtime=ev.time))
                    inlinks[target].append(links[-1])
                    del lastfinishforproc[target]
                continue
            if ('udp_queue_rcv_skb' in irq_wakeup or 'tcp_rcv_established' in irq_wakeup or 'tcp_finish_connect' in irq_wakeup) and source in justreceived and 'tcp_transmit_skb' not in irq_wakeup and 'udp_sendmsg' not in irq_wakeup:
                links.append(struct(sourcerun=justreceived[source],source=justreceived[source].proc,target=target,start=ev.time,stack=ev.stack,outtime=ev.time))
                inlinks[target].append(links[-1])
                del justreceived[source]
                continue
            if 'do_IRQ' in irq_wakeup:
                if source in lastinterruption:
                    wokenbyinterrupt[target]=lastinterruption[source]
                    del lastinterruption[source]
                else:
                    wokenbyinterrupt[target]='unknown'
                continue
            if 'smp_reschedule_interrupt' in irq_wakeup:
                wokenbyinterrupt[target]='IPI'
                continue
            if 'apic_timer_interrupt' in irq_wakeup:
                wokenbyinterrupt[target]='timeout'
                continue
            links.append(struct(source=source,target=target,start=ev.time,stack=ev.stack))
            inlinks[target].append(links[-1])
            if ev.event=='sched:sched_wakeup' and ev.pid==int(ev.args.pid): #exactly what a process waking itself means is unclear, but it happens
                links[-1].outtime=ev.time
            else:
                outlinks[source].append(links[-1])
        elif ev.event=='block:block_bio_queue':
            pass # keep this here so we don't spew error messages on old traces
        elif ev.event=='sched:sched_process_exec':
            source='%s(%d)'%(ev.oldcomm,ev.pid)
            target='%s(%d)'%(ev.comm,ev.pid)
            links.append(struct(source=source, start=ev.time, target=target, end=ev.time,outtime=ev.time,horizontal=True))
            if source in switchedin and switchedin[source]!=-1:
                runs[source].append(struct(start=switchedin[source], end=ev.time))
                for inlink in inlinks[source]:
                    runs[source][-1].inlink=inlink
                    inlink.targetrun=runs[source][-1]
                    inlinks[source]=[]
                if sleeps[source]:
                    runs[source][-1].prev=sleeps[source][-1]
                links[-1].sourcerun=runs[source][-1]
                runs[source][-1].horizoutlink=links[-1]
            inlinks[target].append(links[-1])
            switchedin[source]=-1
            switchedin[target]=ev.time
            runningstacks[target]=[]
        elif ev.event=='cycles' or ev.event=='cpu-clock':
            if ev.comm=='swapper':
                continue # There are several swappers and the cycles event doesn't distinguish
            proc='%s(%d)'%(ev.comm,ev.pid)
            if proc in runningstacks:
                runningstacks[proc].append(ev.stack)
            else:
                if proc in runs:
                    if ev.time-runs[proc][-1].end < 2e-5:
                        runs[proc][-1].stacks.append(ev.stack)
                    else:
                        print 'WARNING: dropping sample for %s at %f which left %d us ago according to sched events'%(proc,ev.time,int(1e6*(ev.time-runs[proc][-1].end)))
                else:
                    runningstacks[proc]=[ev.stack]
        elif ev.event=='block:block_rq_insert':
            callerproc='%s(%d)'%(ev.comm,ev.pid)
            dev=ev.args.raw[0]
            typ=decode_rwbs(ev.args.raw[1])
            offset=ev.args.raw[4]
            size=ev.args.raw[6]
            if dev not in activedevs:
                proc=dev
            else:
                i=1
                while '%s/%d'%(dev,i) in activedevs:
                    i+=1
                proc='%s/%d'%(dev,i)
            activedevs[proc]=1
            if dev+'/'+offset in activebios:
                if 'end' not in activebios[dev+'/'+offset].__dict__:
                    activebios[dev+'/'+offset].end=ev.time
                bios.append(activebios[dev+'/'+offset])
            activebios[dev+'/'+offset]=struct(proc=proc, start=ev.time, repframe='%s of %s blocks at %s'%(typ,size,offset), type='queue', behalfof=callerproc, iotype=typ,dev=dev)
            links.append(struct(start=ev.time, end=ev.time, source=callerproc, target=proc, targetrun=activebios[dev+'/'+offset]))
            outlinks[callerproc].append(links[-1])
        elif ev.event=='block:block_rq_issue':
            dev=ev.args.raw[0]
            offset=ev.args.raw[4]
            if dev+'/'+offset not in activebios:
                print 'WARNING: io issue offset not matched at %f, trying 0...'%ev.time
                #offset='0'
                #if dev+'/'+offset not in activebios:
                #    print 'WARNING: stillunstarted io issue at %f'%ev.time
                continue
                #else:
                #    print "...ok"
            queue = activebios[dev+'/'+offset]
            queue.end=ev.time
            bios.append(queue)
            run=copy(queue)
            run.start=ev.time
            run.type='bio'
            del run.end
            activebios[dev+'/'+offset]=run
            run.prev=queue
            if dev not in lastfinishondev or queue.start>lastfinishondev[dev].end:
                links.append(struct(start=ev.time, end=ev.time, outtime=ev.time, source=queue.proc, target=run.proc, horizontal=True, sourcerun=queue, targetrun=run))
                queue.outlink=links[-1]
                run.inlink=links[-1]
            else:
                lf=lastfinishondev[dev]
                links.append(struct(start=lf.end, end=ev.time, outtime=ev.time, source=lf.proc, target=run.proc, sourcerun=lf, targetrun=run))
                lf.outlink=links[-1]
                run.inlink=links[-1]
        elif ev.event=='block:block_rq_complete':            
            dev=ev.args.raw[0]
            offset=ev.args.raw[3]
            if dev+'/'+offset not in activebios:
                print 'WARNING: unstarted io completion at %f'%ev.time
                continue
            run = activebios[dev+'/'+offset]
            run.end = ev.time
            if run.type=='queue':
                print 'WARNING: finishing unissued bio at %f'%ev.time
                run.type='bio'
            lastfinishondev[dev]=run
            #del activebios[dev+'/'+offset]
            if run.proc in activedevs:
                del activedevs[run.proc]
            lastfinishforproc[run.behalfof]=run
        elif ev.event=='irq:irq_handler_entry':
            proc='%s(%s)'%(ev.comm,ev.pid)
            lastinterruption[proc]=ev.args.name            
        elif ev.event in ['probe:tcp_v4_connect', 'probe:tcp_sendmsg', 'probe:udp_sendmsg']:
            proc='%s(%s)'%(ev.comm,ev.pid)
            if ev.event=='probe:udp_sendmsg':
                dev='udp'
            else:
                dev='tcp'
            dev+=' sock: %s' % ev.args.sk
            if dev not in activenet or 'end' in activenet[dev].__dict__:
                activenet[dev]=struct(start=ev.time, type='bio', proc=dev, dev=dev)
                if ev.event=='probe:tcp_v4_connect':
                    activenet[dev].repframe='connect'
                else:
                    activenet[dev].repframe='content'
                activenet[dev].iotype=activenet[dev].repframe
            bios.append(activenet[dev])
            links.append(struct(target=dev,targetrun=activenet[dev],start=ev.time,end=ev.time,source=proc))
            outlinks[proc].append(links[-1])
        elif ev.event in ['probe:tcp_rcv_established', 'probe:tcp_finish_connect', 'probe:udp_queue_rcv_skb']:
            proc='%s(%s)'%(ev.comm,ev.pid)
            if ev.event=='probe:udp_queue_rcv_skb':
                dev='udp'
            else:
                dev='tcp'
            dev+=' sock: %s' % ev.args.sk
            if dev not in activenet:
                continue
            activenet[dev].end=ev.time
            justreceived[proc]=activenet[dev]
        elif ev.event[:11]=='probe_libc:':
            proc='%s(%s)'%(ev.comm,ev.pid)
            fn=ev.event[11:]
            lastlibc[proc]=struct(function=fn, stack=ev.stack,time=ev.time)
        else:
            print 'ERROR: unhandled event "%s"'%ev.event

    # Only include completed links
    links = [link for link in links if 'end' in link.__dict__];

    # Finish ongoing runs
    for p in switchedin:
        if switchedin[p]!=-1:
            runs[p].append(struct(start=switchedin[p], end=endtime))
            for ol in outlinks[p]:
                ol.sourcerun=runs[p][-1]
                ol.outtime=endtime

    for cp in activebios:
        if 'end' not in activebios[cp].__dict__:
            activebios[cp].end=endtime
        bios.append(activebios[cp])

    for sock in activenet:
        if 'end' not in activenet[sock].__dict__ or not activenet[sock].end:
            activenet[sock].end=endtime

    # Create repframes for sleeps
    for p in sleeps:
        for s in sleeps[p]:
            for frame in s.stack:
                if frame.file!='[kernel.kallsyms]':
                    s.repframe=frame.function
                    break
            if 'repframe' not in s.__dict__ and len(s.stack)>0: # all kernel
                s.repframe = s.stack[0].function

    # Mark boxes with types
    for p in runs:
        for r in runs[p]:
            r.proc=p
            r.type='run'
    for p in sleeps:
        for s in sleeps[p]:
            s.proc=p
            s.type='sleep'

    # Mark links as transfer
    threshold=1e-4
    for l in links:
        if 'outtime' in l.__dict__:
            if l.outtime-l.start<threshold:
                l.istransfer=True
            else:
                l.istransfer=False
        else:
            l.istransfer=False
            print 'no outtime at %f (%s->%s)'%(l.start,l.source,l.target)
            l.outtime=l.start

    boxes=[]
    for p in runs:
        boxes+=runs[p]
    for p in sleeps:
        boxes+=sleeps[p]
    boxes+=bios

    for b in boxes:
        b.wdata=defaultdict(lambda:struct())

    procs=set(runs.keys()) | set(sleeps.keys())
    for b in bios:
        procs.add(b.proc)
    
    return struct(runs=runs, 
                  sleeps=sleeps, 
                  bios=bios,
                  links=links, 
                  boxes=boxes, 
                  procs=procs, 
                  evs=evs,
                  starttime=starttime, 
                  endtime=endtime)
