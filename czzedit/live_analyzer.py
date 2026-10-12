"""Rolling LIVE evaluation using the exact VOD rules and candidate state machine."""
import math
from .common import Config
from .detector import WindowRules, CandidateState
from .viewers import Baseline


class LiveAnalyzer:
    def __init__(self, start_ms, config=None):
        self.rules = WindowRules(config or Config())
        self.rules.gap(0,start_ms)
        self.groups = CandidateState(self.rules)
        self.watermark = start_ms
        self.next_t = max(10000,math.ceil(start_ms/5000)*5000)
        self.viewers = Baseline([]) if self.rules.config.viewer_adaptive else None
        self.last_window = None
        self.pending = []

    def add(self, rows):
        self.rules.add([e for e in rows if not e.get('late') and e['time_ms']>=self.watermark and e.get('time_quality')!='invalid'])

    def gap(self, start, end):
        self.rules.gap(start,end)

    def sample(self, sample):
        if self.viewers:
            self.viewers.append_samples([sample])

    def advance(self, end_ms, final=False):
        if end_ms < self.watermark:
            return []
        result = []
        while self.next_t <= end_ms:
            t = self.next_t
            viewer = self.viewers.at(t) if self.viewers else None
            window,strong = self.rules.evaluate(t,viewer)
            self.last_window = {k:v for k,v in window.items() if k!='evidence'}
            self.pending.extend(self.groups.step(window,strong,t+self.rules.config.post_roll_ms))
            self.next_t += 5000
        self.watermark = end_ms
        if final:
            if end_ms>=10000 and end_ms%5000:
                window,strong = self.rules.evaluate(end_ms,self.viewers.at(end_ms) if self.viewers else None)
                self.pending.extend(self.groups.step(window,strong,end_ms+self.rules.config.post_roll_ms))
            self.pending.extend(self.groups.finish(end_ms))
        waiting=[]
        for marker in self.pending:
            due=marker['reaction_end_ms']+self.rules.config.post_roll_ms
            if final or due<=end_ms:
                marker=dict(marker)
                if marker['clip_end_ms']>end_ms:
                    marker['clip_end_ms']=end_ms
                if due>end_ms or any(a<marker['clip_end_ms'] and b>marker['clip_start_ms'] for a,b in self.rules.gaps):
                    marker['partial_context']=True
                result.append(marker)
            else: waiting.append(marker)
        self.pending=waiting
        self.rules.prune(end_ms-1000000)
        return result

    def previews(self):
        values=[dict(m,state='waiting_context',clip_end_ms=min(m['clip_end_ms'],self.watermark),partial_context=True) for m in self.pending]
        active=self.preview()
        if active: values.append(active)
        return values

    def preview(self):
        if self.groups.active:
            marker = self.groups.marker(self.groups.active,self.watermark,partial=True)
            if marker:
                marker['state'] = 'generating' if self.watermark<=marker['reaction_end_ms']+10000 else 'waiting_context'
            return marker
        return None
