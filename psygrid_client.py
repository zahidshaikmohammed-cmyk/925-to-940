from __future__ import annotations
import gzip,json
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import dataclass
from datetime import datetime,time as dtime
from urllib.request import Request,urlopen
from zoneinfo import ZoneInfo
from strategy_930 import Candle
IST=ZoneInfo('Asia/Kolkata'); SHARDS=tuple(f'live-{x}.json' for x in 'abcdefghij')
@dataclass(frozen=True)
class Health:
    symbol:str; healthy:bool; reason:str=''
@dataclass(frozen=True)
class StockData:
    symbol:str; candles:tuple[Candle,...]; ltp:float; previous_close:float|None; health:Health
class PsygridClient:
    def __init__(self,base_url:str,timeout:float=4.0):self.base=base_url.rstrip('/');self.timeout=timeout
    def _get(self,path):
        req=Request(self.base+'/'+path.lstrip('/'),headers={'Accept':'application/json','Accept-Encoding':'gzip','Cache-Control':'no-cache'})
        with urlopen(req,timeout=self.timeout) as r:
            body=r.read()
            if r.headers.get('Content-Encoding','').lower()=='gzip':body=gzip.decompress(body)
            return json.loads(body.decode('utf-8'))
    def market(self):
        out={}; errors=[]
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs={ex.submit(self._get,f'public/{s}'):s for s in SHARDS}
            for f in as_completed(futs):
                shard=futs[f]
                try:
                    p=f.result(); stocks=p.get('stocks')
                    if p.get('stock_count')!=45 or not isinstance(stocks,dict):raise RuntimeError(f'{shard}: invalid shard')
                    overlap=set(out)&set(stocks)
                    if overlap:raise RuntimeError(f'{shard}: duplicate {sorted(overlap)[:3]}')
                    out.update(stocks)
                except Exception as e:errors.append(str(e))
        if errors:raise RuntimeError('; '.join(errors))
        if len(out)!=450:raise RuntimeError(f'expected 450 stocks, got {len(out)}')
        return out
    @staticmethod
    def _ts(x):
        if isinstance(x,(int,float)):return datetime.fromtimestamp(float(x),IST)
        s=str(x).replace(' IST','').strip(); dt=datetime.fromisoformat(s.replace('Z','+00:00'))
        return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
    @classmethod
    def candles(cls,rows):
        out=[]
        for r in rows if isinstance(rows,list) else []:
            if r.get('complete',True) is False:continue
            try:c=Candle(cls._ts(r['timestamp']),float(r['open']),float(r['high']),float(r['low']),float(r['close']),float(r['volume']))
            except Exception:continue
            out.append(c)
        return tuple(sorted(out,key=lambda c:c.ts))
    def stock(self,symbol,payload,now=None):
        now=now or datetime.now(IST); reasons=[]; cs=self.candles(payload.get('1m',[])); ltp=float(payload.get('ltp') or 0)
        if ltp<=0:reasons.append('invalid_ltp')
        ltt=payload.get('ltp_timestamp')
        if not ltt:reasons.append('missing_ltp_timestamp')
        else:
            age=(now-self._ts(ltt)).total_seconds()
            if age>10:reasons.append(f'stale_ltp_{age:.1f}s')
        grid=[datetime.combine(now.date(),dtime(9,15+i),IST) for i in range(15)]
        got={c.ts.replace(second=0,microsecond=0) for c in cs if c.ts.date()==now.date() and c.ts.time()<dtime(9,30)}
        if len(got)!=15 or any(x not in got for x in grid):reasons.append('missing_09_15_to_09_29_grid')
        cs=tuple(c for c in cs if c.ts.date()==now.date() and dtime(9,15)<=c.ts.time()<dtime(9,30))
        prev=None
        for r in self.candles(payload.get('15m',[])):
            if r.ts.date()<now.date() and r.ts.time()<=dtime(15,15):prev=r.close
        if prev is None:reasons.append('missing_previous_close')
        health=Health(symbol,not reasons,';'.join(reasons)); return StockData(symbol,cs,ltp,prev,health)
