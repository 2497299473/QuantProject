# -*- coding: utf-8 -*-
"""判别当前出口是「全域被掐」还是「仅 kline 接口被掐」：每个 URL 只发 1 次，无重试。
参照组选 home IP 上实测 200 的 kamt 与 quote 同域 newapi —— 若这两者在新 IP 也 200
而 kline 仍死，则封禁是接口/客户端类维度，换 IP 无解。"""
import json
import time
import urllib.request

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

CASES = [
    ('kamt 参照(家宽曾200)',
     'http://push2.eastmoney.com/api/qt/kamt/get'
     '?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54&ut=fa5fd1943c7b386f172d6893dbfba10b'),
    ('newapi 参照(家宽曾200)',
     'http://quote.eastmoney.com/newapi/bk/cyzh/BK0428'),
    ('kline 目标(家宽被掐)',
     'http://push2his.eastmoney.com/api/qt/stock/kline/get'
     '?secid=90.BK0428&klt=101&fqt=1&beg=20150101&end=20500101'
     '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56'),
]

for name, url in CASES:
    tag = 'FAIL'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA,
                                                   'Referer': 'http://quote.eastmoney.com/'})
        with urllib.request.urlopen(req, timeout=12) as r:
            b = r.read()
        ok = len(b) > 20
        tag = 'OK  ' if ok else 'EMPTY'
        print('%s %-22s bytes=%d head=%s' % (tag, name, len(b), b[:70].decode('utf-8', 'replace')))
    except Exception as e:
        print('%s %-22s %s: %s' % (tag, name, type(e).__name__, str(e)[:80]))
    time.sleep(1.5)
