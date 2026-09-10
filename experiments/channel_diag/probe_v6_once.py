# -*- coding: utf-8 -*-
"""移动网 IPv6 单请求探针：强制 AF_INET6 连 push2his 的 AAAA，1 次 GET 无重试。"""
import http.client
import json
import socket
from datetime import datetime

PATH = ('/api/qt/stock/kline/get?secid=90.BK0486&klt=101&fqt=1'
        '&beg=20150101&end=20500101&fields1=f1&fields2=f51,f52,f53,f54,f55,f56')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')

print('probe v6 |', datetime.now().astimezone().isoformat(timespec='seconds'))
try:
    infos = socket.getaddrinfo('push2his.eastmoney.com', 80, socket.AF_INET6)
    v6 = sorted({i[4][0] for i in infos})
    print('AAAA:', v6)
except Exception as e:
    print('AAAA resolve FAIL', e)
    v6 = []

for ip in v6[:2]:
    try:
        s = socket.create_connection((ip, 80), timeout=10)
        c = http.client.HTTPConnection(ip, 80, timeout=10)
        c.sock = s
        c.putrequest('GET', PATH)
        c.putheader('Host', 'push2his.eastmoney.com')
        c.putheader('User-Agent', UA)
        c.putheader('Referer', 'http://quote.eastmoney.com/')
        c.endheaders()
        r = c.getresponse()
        b = r.read()
        j = json.loads(b.decode('utf-8', 'replace'))
        kl = (j.get('data') or {}).get('klines') or []
        print('V6 %s -> HTTP %s bytes=%d bars=%d %s' % (
            ip, r.status, len(b), len(kl),
            kl[-1].split(',')[0] if kl else '-'))
    except Exception as e:
        print('V6 %s -> FAIL %s: %s' % (ip, type(e).__name__, str(e)[:70]))
