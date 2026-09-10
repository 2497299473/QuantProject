# -*- coding: utf-8 -*-
"""单码通道诊断：区分「东财 IP 频控」与「CORS 跨域拦截」。

背景（2026-09-08 16:00 事件）：
- 原生 HTTP：64/65 RemoteDisconnected（连接层被掐）
- Playwright 页面内 fetch：64/64 TypeError: Failed to fetch
- 疑点：pull_sector_klines_pw.py 把页面开在 quote.eastmoney.com，
  fetch 目标却是 push2his.eastmoney.com —— 跨域。
  浏览器把 CORS 拦截和网络失败都报成 "Failed to fetch"，现有脚本无法区分。

四个测试（同一 BK 码，请求间隔 >=2s）：
A 原生 urllib + 浏览器 UA            —— 复核当前频控是否仍在
B headless 直接 goto 接口 URL（顶层导航，无 CORS）—— 判别器
C 复现现脚本：quote 页内 fetch push2his —— 看原始 error 文本
D JSONP（cb= 回调，script 标签注入，无 CORS）—— 备选修复路径

判读：
- B 成功 → 不是 IP 频控，是 CORS/指纹问题，兜底脚本改「导航取数」即可救活
- B 失败且报 net::ERR_* → 确实 IP/连接层被掐，本地脚本换姿势无用
用法：D:\Python\python.exe -X utf8 experiments\channel_diag\diag_20260908.py [BK0428]
"""
import json
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BK = sys.argv[1] if len(sys.argv) > 1 else 'BK0428'
API = ('https://push2his.eastmoney.com/api/qt/stock/kline/get'
       '?secid=90.' + BK +
       '&klt=101&fqt=1&beg=20150101&end=20500101'
       '&fields1=f1&fields2=f51,f52,f53,f54,f55,f56')
QUOTE = 'https://quote.eastmoney.com/bk/90.' + BK + '.html'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36')


def head(t):
    print('\n===== ' + t + ' =====', flush=True)


def summarize(text):
    """把返回体压成一行：状态 + bars + 末根日期。"""
    try:
        data = json.loads(text)
        ks = (data.get('data') or {}).get('klines')
        if not ks:
            return 'EMPTY/异常 json=' + text[:90].replace('\n', ' ')
        return 'OK bars=%d last=%s' % (len(ks), ks[-1].split(',')[0])
    except Exception:
        # JSONP 回调格式：cbName({...})
        s = text.strip()
        if s.endswith(')') and '(' in s:
            try:
                payload = json.loads(s[s.index('(') + 1: -1])
                ks = (payload.get('data') or {}).get('klines')
                if ks:
                    return 'OK(jsonp) bars=%d last=%s' % (len(ks), ks[-1].split(',')[0])
            except Exception:
                pass
        return 'NONJSON len=%d head=%s' % (len(text), text[:90].replace('\n', ' '))


def main():
    print('diag target:', BK, '|', time.strftime('%F %T %z'), flush=True)

    # ---- A 原生 urllib ----
    head('A native urllib + UA')
    req = urllib.request.Request(API, headers={
        'User-Agent': UA,
        'Referer': 'https://quote.eastmoney.com/',
        'Accept': 'application/json, text/plain, */*',
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print('A status=%d %s' % (r.status, summarize(r.read().decode('utf-8', 'replace'))), flush=True)
    except urllib.error.HTTPError as e:
        print('A HTTPError=%s' % e.code, flush=True)
    except Exception as e:
        print('A FAIL %s: %s' % (type(e).__name__, str(e)[:120]), flush=True)
    time.sleep(3)

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(user_agent=UA)
        pg = ctx.new_page()

        # ---- B 顶层导航直取接口（无 CORS）----
        head('B headless goto(api_url)  [CORS-free discriminator]')
        try:
            resp = pg.goto(API, wait_until='domcontentloaded', timeout=30000)
            body = pg.evaluate('() => document.body ? document.body.innerText : ""')
            print('B status=%s body_len=%d %s' % (
                resp and resp.status, len(body or ''), summarize(body or '')), flush=True)
            if not (body or '').strip():
                print('B (body 空，可能 ERR_EMPTY_RESPONSE/被重置)', flush=True)
        except Exception as e:
            print('B FAIL %s' % str(e)[:200], flush=True)
        time.sleep(3)

        # ---- C 复现现脚本：页面内 fetch ----
        head('C in-page fetch from quote host (repro of pull_sector_klines_pw)')
        try:
            if BK + '.html' not in (pg.url or ''):
                pg.goto(QUOTE, wait_until='domcontentloaded', timeout=30000)
            print('C page_url=%s' % pg.url, flush=True)
        except Exception as e:
            print('C goto quote FAIL %s' % str(e)[:150], flush=True)
        pg.wait_for_timeout(2500)
        res = pg.evaluate('''async (u) => {
            try {
                const r = await fetch(u, {credentials:'include'});
                return {status: r.status, text: await r.text()};
            } catch (e) { return {error: String(e)}; }
        }''', API)
        if res.get('error'):
            print('C FAIL error=%s' % res['error'][:150], flush=True)
        else:
            print('C status=%s %s' % (res.get('status'), summarize(res.get('text') or '')), flush=True)
        time.sleep(3)

        # ---- D JSONP（script 标签，无 CORS）----
        head('D JSONP via <script> injection (cb= param)')
        res = pg.evaluate('''async (base) => new Promise((resolve) => {
            const name = '__diag_cb';
            const s = document.createElement('script');
            const to = setTimeout(() => resolve({error:'jsonp timeout 15s'}), 15000);
            const clean = () => { clearTimeout(to); try { s.remove(); } catch (e) {} try { delete window[name]; } catch (e) {} };
            window[name] = (data) => { clean(); resolve({json: JSON.stringify(data)}); };
            s.onerror = () => { clean(); resolve({error:'script onerror (network-level)'}); };
            s.src = base + '&cb=' + name;
            document.head.appendChild(s);
        })''', API)
        if res.get('error'):
            print('D FAIL %s' % res['error'][:150], flush=True)
        else:
            print('D OK %s' % summarize(res.get('json') or ''), flush=True)

        # ---- E 看响应头是否带 ACAO（只有请求真到达服务器时才有意义）----
        head('E access-control-allow-origin header probe')
        try:
            r2 = pg.request.get(API, headers={'Referer': QUOTE, 'User-Agent': UA}, timeout=25000)
            hdrs = r2.headers
            print('E status=%s acao=%s server=%s len=%d' % (
                r2.status, hdrs.get('access-control-allow-origin'),
                hdrs.get('server'), len(r2.body())), flush=True)
        except Exception as e:
            print('E FAIL %s' % str(e)[:180], flush=True)

        ctx.close()
        b.close()

    print('\ndiag done |', time.strftime('%F %T %z'), flush=True)


if __name__ == '__main__':
    main()
