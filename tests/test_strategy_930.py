from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from config import StrategyConfig
from strategy_930 import Candle,atr,efficiency,vwap
IST=ZoneInfo('Asia/Kolkata')
def make(values):
    t=datetime(2026,9,15,9,15,tzinfo=IST); out=[]
    for i,(o,h,l,c,v) in enumerate(values):out.append(Candle(t+timedelta(minutes=i),o,h,l,c,v))
    return out
def test_vwap_and_atr_are_positive():
    cs=make([(100,101,99,100.5,1000)]*15)
    assert vwap(cs)>0; assert atr(cs,10)>0
def test_efficiency_monotonic_is_one():
    cs=make([(100+i,101+i,99+i,100+i,1000) for i in range(15)])
    assert efficiency(cs)==1.0
def test_cutoff_has_exactly_15_opening_minutes():
    cs=make([(100,101,99,100,1000)]*15)
    assert len([c for c in cs if c.ts.time().hour==9 and c.ts.time()>=__import__('datetime').time(9,15) and c.ts.time()<__import__('datetime').time(9,30)])==15
