# -*- coding: utf-8 -*-
"""东财 kline 单请求探针：1 次 HTTP 明文 GET，无重试、无 netutil 链路。
通了再跑全量补拉，不通立刻停。"""
import json
import urllib.request

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
URL = ('http://push2his.eastmoney.com/api/qt/stock/kline/get'
       '?secid=90.BK0428&klt=101&fqt=1&beg=20150101&end=20500101'
       '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56')

try:
    req = urllib.request.Request(URL, headers={'User-Agent': UA,
                                               'Referer': 'http://quote.eastmoney.com/'})
    with urllib.request.urlopen(req, timeout=12) as r:
        j = json.loads(r.read().decode('utf-8'))
    kl = (j.get('data') or {}).get('klines') or []
    if kl:
        print('PROBE OK bars=%d first=%s last=%s'
              % (len(kl), kl[0].split(',')[0], kl[-1].split(',')[0]))
    else:
        print('PROBE EMPTY (connected but no data):', str(j)[:120])
except Exception as e:
    print('PROBE FAIL %s: %s' % (type(e).__name__, str(e)[:100]))
