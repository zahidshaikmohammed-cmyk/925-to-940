from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,time
from math import isfinite
from statistics import median
from typing import Iterable
from zoneinfo import ZoneInfo
from config import StrategyConfig
IST=ZoneInfo('Asia/Kolkata'); CUTOFF=time(9,30)
@dataclass(frozen=True)
class Candle: ts:datetime; open:float; high:float; low:float; close:float; volume:float
@dataclass(frozen=True)
class Candidate:
    symbol:str; side:str; score:float; entry:float; stop:float; target:float; gap_pct:float; impulse_pct:float; impulse_atr:float; retracement_depth:float; retracement_volume_ratio:float; rs_market:float; rs_sector:float; vwap_distance_atr:float; structure:float; reasons:tuple[str,...]
def finite_ohlcv(c):return all(isfinite(x) for x in (c.open,c.high,c.low,c.close,c.volume)) and min(c.open,c.high,c.low,c.close)>0 and c.volume>=0 and c.high>=max(c.open,c.close) and c.low<=min(c.open,c.close)
def atr(cs,p):
    prev=None;trs=[]
    for c in cs:trs.append(c.high-c.low if prev is None else max(c.high-c.low,abs(c.high-prev),abs(c.low-prev)));prev=c.close
    return median(trs[-p:]) if trs else 0.0
def vwap(cs):
    den=sum(c.volume for c in cs);return sum(((c.high+c.low+c.close)/3)*c.volume for c in cs)/den if den else sum(c.close for c in cs)/len(cs)
def efficiency(cs):
    if len(cs)<2:return 0.0
    path=sum(abs(cs[i].close-cs[i-1].close) for i in range(1,len(cs)));return abs(cs[-1].close-cs[0].open)/path if path else 0.0
def clamp(x,lo=0,hi=1):return max(lo,min(hi,x))
def scale(x,lo,hi):return clamp((x-lo)/(hi-lo)) if hi>lo else 0
def robust_z(x,sample):
    xs=[v for v in sample if isfinite(v)]
    if len(xs)<5:return 0.0
    m=median(xs);mad=median(abs(v-m) for v in xs);return 0.0 if mad==0 else .6744897501960817*(x-m)/mad
def _leg(cs,side,cfg):
    start=cs[0].open; idx=(max if side==1 else min)(range(len(cs)),key=lambda i:cs[i].high if side==1 else cs[i].low)
    if idx+1<len(cs) and idx<cfg.min_impulse_bars-1:return None
    extreme=cs[idx].high if side==1 else cs[idx].low;post=cs[idx+1:]
    if (extreme<=start if side==1 else extreme>=start) or len(post)<cfg.min_retracement_bars:return None
    retrace=min(c.low for c in post) if side==1 else max(c.high for c in post);den=abs(extreme-start)
    depth=abs(extreme-retrace)/den;reclaim=(cs[-1].close-retrace)/(extreme-retrace) if side==1 else (retrace-cs[-1].close)/(retrace-extreme)
    return idx,extreme,retrace,depth,reclaim,side*100*(extreme/start-1)
def evaluate(symbol,candles,entry,previous_close,market_return,sector_return,gap_history,cfg):
    cs=sorted([c for c in candles if c.ts.astimezone(IST).time()<CUTOFF],key=lambda c:c.ts)[-15:]
    if len(cs)!=15 or any(not finite_ohlcv(c) for c in cs):return None
    a=atr(cs,cfg.atr_period)
    if a<=0:return None
    stock_ret=100*(cs[-1].close/cs[0].open-1);gap=100*(cs[0].open/previous_close-1);gz=robust_z(gap,gap_history);vw=vwap(cs);best=None
    for side,name in ((1,'LONG'),(-1,'SHORT')):
        leg=_leg(cs,side,cfg)
        if not leg:continue
        idx,extreme,retrace,depth,reclaim,impulse_pct=leg;impulse_atr=abs(extreme-cs[0].open)/a;rs=side*(stock_ret-market_return);srs=side*(stock_ret-sector_return);iv=cs[:idx+1];rv=cs[idx+1:]
        vol_ratio=(sum(c.volume for c in rv)/len(rv))/(sum(c.volume for c in iv)/len(iv) or 1);eff=efficiency(iv);vw_dist=side*(cs[-1].close-vw)/a;extension=abs(cs[-1].close-vw)/a;persistence=sum(1 for c in cs[1:] if side*(c.close-c.open)>0)/14
        if abs(gap)>cfg.max_gap_pct or abs(gz)>cfg.max_gap_z or impulse_pct<cfg.min_impulse_pct:continue
        if idx>cfg.late_extreme_bar or not(cfg.min_retracement_depth<=depth<=cfg.max_retracement_depth) or reclaim<cfg.min_reclaim_ratio:continue
        if impulse_atr>cfg.max_impulse_atr or extension>cfg.max_extension_from_vwap_atr or vw_dist<=0:continue
        if eff<cfg.min_directional_efficiency or vol_ratio>cfg.max_retracement_volume_ratio or persistence<cfg.min_persistence:continue
        if impulse_atr>=3 and abs(cs[-1].close-extreme)/a<cfg.exhaustion_reclaim_distance_atr:continue
        si=scale(impulse_pct,cfg.min_impulse_pct,1.5);sr=clamp(1-abs(depth-.52)/.22);sm=scale(rs,.1,.8);ssr=scale(srs,.05,.6);sv=scale(1-vol_ratio,0,.5);sw=scale(vw_dist,0,1.25);st=.5*clamp(eff)+.5*clamp(reclaim);sx=scale(impulse_atr,.5,2.5)
        score=100*(cfg.w_impulse*si+cfg.w_retracement*sr+cfg.w_relative_strength*sm+cfg.w_sector_strength*ssr+cfg.w_volume*sv+cfg.w_vwap*sw+cfg.w_structure*st+cfg.w_volatility*sx)
        stop=retrace-cfg.stop_atr_buffer*a if side==1 else retrace+cfg.stop_atr_buffer*a;risk=abs(entry-stop);target=entry+side*max(cfg.minimum_rr*risk,cfg.minimum_target_atr*a)
        cand=Candidate(symbol,name,score,entry,stop,target,gap,impulse_pct,impulse_atr,depth,vol_ratio,rs,srs,vw_dist,st,('ELIGIBLE',))
        if best is None or cand.score>best.score:best=cand
    return best
def rank(candidates:Iterable[Candidate],side):
    xs=[c for c in candidates if c.side==side];return sorted(xs,key=lambda c:(-c.score,-c.rs_market,-c.rs_sector,c.vwap_distance_atr,c.symbol))[0] if xs else None
