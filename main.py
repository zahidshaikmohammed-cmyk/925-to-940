from __future__ import annotations
import json,time
from datetime import datetime,dtime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo
from config import StrategyConfig
from psygrid_client import PsygridClient
from strategy_930 import evaluate,rank

IST=ZoneInfo('Asia/Kolkata'); BASE='http://140.245.226.102:10000'

def beep():
    print('\a',end='',flush=True)
    try:
        import winsound; winsound.Beep(1200,350)
    except Exception: pass

def sleep_until(target):
    while True:
        left=(target-datetime.now(IST)).total_seconds()
        if left<=0:return
        time.sleep(min(0.5,max(0.05,left/4)))

def load_sector_map():
    p=Path('sector_map.json')
    if not p.exists():return {}
    try:return json.loads(p.read_text(encoding='utf-8'))
    except Exception:return {}

def print_candidate(title,c):
    if not c:
        print(f'\n{title}: NO QUALIFIED CANDIDATE')
        return
    print(f'''\n============================================================\n{title}\n============================================================\nSYMBOL       : {c.symbol}\nDIRECTION    : {c.side}\nSCORE        : {c.score:.2f}/100\nENTRY(09:31) : ₹{c.entry:.4f}\nSTOP LOSS    : ₹{c.stop:.4f}\nTARGET       : ₹{c.target:.4f}\n\nGAP          : {c.gap_pct:+.3f}%\nIMPULSE      : {c.impulse_pct:+.3f}%\nIMPULSE ATR  : {c.impulse_atr:.2f}\nRETRACEMENT  : {c.retracement_depth*100:.1f}%\nRETRACE VOL  : {c.retracement_volume_ratio:.2f}x impulse\nRS MARKET    : {c.rs_market:+.3f}%\nRS SECTOR    : {c.rs_sector:+.3f}%\nVWAP EXT     : {c.vwap_distance_atr:.2f} ATR\nSTRUCTURE    : {c.structure:.2f}\n============================================================''')

def main():
    cfg=StrategyConfig(); cfg.validate(); client=PsygridClient(BASE); sectors=load_sector_map()
    now=datetime.now(IST); today=now.date()
    start=datetime.combine(today,dtime(9,15),IST); signal=datetime.combine(today,dtime(9,30),IST); entry_time=datetime.combine(today,dtime(9,31),IST)
    if now < start:
        print(f'PSYGRID 09:30 ENGINE | waiting for 09:15 IST'); sleep_until(start)
    elif now >= signal:
        print('09:30 lock time has passed; start the engine before 09:30.')
        return
    print('============================================================'); print(' PSYGRID 09:15 -> 09:30 FORCED LONG/SHORT ENGINE'); print('============================================================')
    print('Monitoring 450 stocks from 09:15. No PSYGRID indicators are used.')
    last_status=0
    while datetime.now(IST)<signal:
        try:
            stocks=client.market()
            healthy=0
            for sym,p in stocks.items():
                d=client.stock(sym,p)
                healthy+=d.health.healthy
            t=time.time()
            if t-last_status>=30:
                print(f'[{datetime.now(IST):%H:%M:%S}] feed OK | 450 shards-universe | healthy currently {healthy}/450')
                last_status=t
        except Exception as e:
            print(f'[{datetime.now(IST):%H:%M:%S}] feed warning: {e}')
        sleep_until(min(signal,datetime.now(IST)+__import__('datetime').timedelta(seconds=3)))

    # Atomic 09:30 snapshot: only 09:15-09:29 completed candles are eligible.
    beep(); print('\n🔔 09:30:00 SIGNAL FREEZE — collecting final 450-stock snapshot...')
    raw=client.market(); data={s:client.stock(s,p,datetime.now(IST)) for s,p in raw.items()}
    healthy={s:d for s,d in data.items() if d.health.healthy}
    returns=[100*(d.candles[-1].close/d.candles[0].open-1) for d in healthy.values() if len(d.candles)==15]
    market_return=median(returns) if returns else 0.0
    candidates=[]; gap_history=[]
    for d in healthy.values():
        if d.previous_close and d.candles:gap_history.append(100*(d.candles[0].open/d.previous_close-1))
    for sym,d in healthy.items():
        stock_ret=100*(d.candles[-1].close/d.candles[0].open-1)
        sector=sectors.get(sym)
        if sector:
            peer=[100*(x.candles[-1].close/x.candles[0].open-1) for s,x in healthy.items() if sectors.get(s)==sector and len(x.candles)==15]
            sector_return=median(peer) if peer else market_return
        else:
            sector_return=market_return
        c=evaluate(sym,list(d.candles),d.ltp,d.previous_close,market_return,sector_return,gap_history,cfg)
        if c:candidates.append(c)
    long=rank(candidates,'LONG'); short=rank(candidates,'SHORT')
    print(f'Healthy stocks: {len(healthy)}/450 | Data-rejected: {450-len(healthy)} | Strategy-qualified: {len(candidates)}')
    if not sectors:print('NOTE: sector_map.json not present; sector RS is using the healthy-universe median proxy.')
    print_candidate('🥇 BEST LONG',long); print_candidate('🥇 BEST SHORT',short)
    print('\n🔒 RANKING FROZEN. No stock can replace these selections after 09:30.')
    sleep_until(entry_time)
    # Refresh only the selected symbols for executable 09:31 LTP.
    for label,c in (('LONG',long),('SHORT',short)):
        if not c:continue
        try:
            p=client._get(f'public/stock/{c.symbol}.json'); live=float(p.get('ltp') or c.entry)
            print(f'[{datetime.now(IST):%H:%M:%S}] {label} {c.symbol} | 09:31 ENTRY LTP = ₹{live:.4f} | SL/TP remain structural.')
        except Exception as e:print(f'[{label}] 09:31 LTP refresh failed: {e}')

if __name__=='__main__':main()
