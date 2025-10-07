from flask import Flask, Response, request, jsonify
import os
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
import time
import re
import json
import redis

# --- Vercel 环境修正 ---
os.environ['HOME'] = '/tmp'
app = Flask(__name__)

# --- Redis Client Initialization ---
try:
    redis_url = os.environ.get('KV_REDIS_URL')
    if not redis_url:
        raise ValueError("KV_REDIS_URL environment variable not found.")
    r = redis.Redis.from_url(redis_url, decode_responses=True) # decode_responses=True is important
except Exception as e:
    print(f"Error initializing Redis: {e}")
    r = None

# --- 默认配置 ---
DEFAULT_PORTFOLIO = {
    '002594.SZ': {'shares': 10000, 'name': '比亚迪'}, '300274.SZ': {'shares': 15000, 'name': '阳光电源'},
    '600895.SH': {'shares': 11600, 'name': '张江高科'}, '09880.HK':  {'shares': 7800,  'name': '优必选'},
    '00981.HK':  {'shares': 6000,  'name': '中芯国际'}
}
DEFAULT_LIABILITIES_CNY = 2527439

session = requests.Session()
session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'})

# --- Timezone Setup ---
CST = timezone(timedelta(hours=8), 'CST')

# --- 数据获���模块 ---
def get_market_data(portfolio):
    a_codes = [c for c in portfolio if c.endswith(('.SH', '.SZ'))]
    hk_codes = [c for c in portfolio if c.endswith('.HK')]
    
    def fetch_sina_data(codes, is_hk=False):
        data = {}
        if not codes: return data
        
        prefix = "hk" if is_hk else ""
        sina_codes = [f"{prefix}{c.split('.')[0]}" if is_hk else f"{c[-2:].lower()}{c[:-3]}" for c in codes]
        url = f"http://hq.sinajs.cn/list={','.join(sina_codes)}"
        
        try:
            headers = {'Referer': 'https://finance.sina.com.cn/'}
            response = session.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            content = response.text
            
            # Get current time in Beijing for comparison
            now_cst = datetime.now(CST)
            today_930_cst = now_cst.replace(hour=9, minute=30, second=0, microsecond=0)

            for i, code in enumerate(codes):
                match = re.search(f'var hq_str_{sina_codes[i]}="(.*?)"', content)
                if not match: continue
                parts = match.group(1).split(',')
                
                if is_hk:
                    price_idx, pre_close_idx, date_idx, time_idx = 6, 3, 17, 18
                    date_format = '%Y/%m/%d'
                else:
                    price_idx, pre_close_idx, date_idx, time_idx = 3, 2, 30, 31
                    date_format = '%Y-%m-%d'

                if len(parts) <= max(price_idx, pre_close_idx, date_idx, time_idx): continue
                
                try:
                    current_price = float(parts[price_idx])
                    pre_close = float(parts[pre_close_idx])
                    
                    # Check market time
                    market_date_str = parts[date_idx]
                    market_time_str = parts[time_idx]
                    market_dt_str = f"{market_date_str} {market_time_str}"
                    
                    # Handle HK market time which may not include seconds
                    time_format = f"{date_format} %H:%M:%S"
                    if is_hk and len(market_time_str.split(':')) == 2:
                        time_format = f"{date_format} %H:%M"

                    market_dt = datetime.strptime(market_dt_str, time_format).replace(tzinfo=CST)

                    # If market time is before 9:30 AM today, treat current price as previous close to zero out P/L
                    if market_dt < today_930_cst:
                        current_price = pre_close
                        print(f"  - {code}: 行情时间 ({market_time_str}) 早于 09:30，盈亏计为0。")

                    if current_price != 0.0 and pre_close != 0.0:
                        data[code] = {'price': current_price, 'pre_close': pre_close}
                except (ValueError, IndexError) as e:
                    print(f"  - 解析 {code} 数据时出错: {e}")
        except Exception as e:
            print(f"获取 {'港股' if is_hk else 'A股'} 数据时出错: {e}")
        return data

    market_data = fetch_sina_data(a_codes)
    market_data.update(fetch_sina_data(hk_codes, is_hk=True))
    return market_data

def get_hkd_cny_rate():
    try:
        response = session.get("https://www.google.com/finance/quote/HKD-CNY", timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        return float(soup.find('div', class_='YMlKec fxKbKc').text)
    except Exception: return 0.9

def get_news_from_sina(portfolio):
    all_news = {}
    print("正在从新浪财经抓取公司要闻...")
    for code, details in portfolio.items():
        if not isinstance(details, dict): continue
        stock_name = details.get('name', '未知股票')
        sina_code = f"{code[-2:].lower()}{code[:-3]}"
        url = f"https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_AllNewsStock/symbol/{sina_code}.phtml"
        news_list = []
        try:
            response = session.get(url, timeout=15)
            response.encoding = 'gbk'
            soup = BeautifulSoup(response.text, 'html.parser')
            news_container = soup.find('div', class_='datelist')
            if news_container:
                for item in news_container.find_all('a', limit=5):
                    title = item.text.strip()
                    link = item['href']
                    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', item.parent.text)
                    date_str = date_match.group(1) if date_match else ""
                    news_list.append({'title': title, 'url': link, 'source_time': date_str})
        except Exception as e:
            print(f"抓取 {stock_name} 新闻时出错: {e}")
        all_news[code] = news_list
    return all_news

# --- 核心逻辑与渲染 ---
def get_report_context(portfolio, liabilities):
    """Fetches all data and performs calculations."""
    market_data = get_market_data(portfolio)
    news_data = get_news_from_sina(portfolio)
    hkd_cny_rate = get_hkd_cny_rate()
    
    all_data = {}
    total_assets_cny = total_pnl_cny = total_pre_close_value_cny = 0

    for code, details in portfolio.items():
        if not isinstance(details, dict) or code not in market_data: continue
        
        price_data = market_data[code]
        is_hk = code.endswith('.HK')
        rate = hkd_cny_rate if is_hk else 1.0
        
        market_value = price_data['price'] * details['shares'] * rate
        pre_close_value = price_data['pre_close'] * details['shares'] * rate
        pnl = market_value - pre_close_value
        
        all_data[code] = {
            **price_data, 'name': details['name'], 'shares': details['shares'],
            'currency': 'HKD' if is_hk else 'CNY', 'market_value_cny': market_value,
            'pnl_cny': pnl, 'pnl_percent': (pnl / pre_close_value) * 100 if pre_close_value else 0
        }
        total_assets_cny += market_value
        total_pnl_cny += pnl
        total_pre_close_value_cny += pre_close_value

    net_worth = total_assets_cny - liabilities
    total_pnl_percent = (total_pnl_cny / total_pre_close_value_cny) * 100 if total_pre_close_value_cny else 0
    
    return {
        "portfolio": portfolio, "liabilities": liabilities, "all_data": all_data,
        "news_data": news_data, "net_worth": net_worth, "total_assets_cny": total_assets_cny,
        "total_pnl_cny": total_pnl_cny, "total_pnl_percent": total_pnl_percent
    }

def render_main_content_html(context):
    """Renders only the dynamic parts of the report (summary and table)."""
    pnl_arrow = '▲' if context['total_pnl_cny'] >= 0 else '▼'
    pnl_class_total = 'pnl-positive' if context['total_pnl_cny'] >= 0 else 'pnl-negative'
    
    portfolio_rows_html = []
    for code, details in context['all_data'].items():
        pnl_class = 'pnl-positive' if details['pnl_cny'] >= 0 else 'pnl-negative'
        pnl_arrow_stock = '▲' if details['pnl_cny'] >= 0 else '▼'
        portfolio_rows_html.append(f"""
        <tr>
            <td><strong>{details['name']}</strong></td><td>{code}</td>
            <td><span class="sensitive-data" data-value="{details['shares']:,}">***</span></td>
            <td>{details['price']:.2f} {details['currency']}</td>
            <td><span class="sensitive-data" data-value="{details['market_value_cny']:,.2f}">***</span></td>
            <td class="{pnl_class}">{pnl_arrow_stock} {abs(details['pnl_cny']):,.2f}</td>
            <td class="{pnl_class}">{pnl_arrow_stock} {abs(details['pnl_percent']):.2f}%</td>
        </tr>""")

    news_html = ""
    for code, news_list in context.get('news_data', {}).items():
        if news_list:
            stock_name = context['all_data'].get(code, {}).get('name', code)
            news_html += f"<h3>{stock_name}</h3><ul>"
            for item in news_list:
                news_html += f"<li><a href='{item['url']}' target='_blank'>{item['title']}</a><span class='news-date'>{item.get('source_time', '')}</span></li>"
            news_html += "</ul>"
    
    return f"""
    <div class="card summary">
        <div class="summary-item"><h3>总资产 (CNY)</h3><p><span class="sensitive-data" data-value="{context['total_assets_cny']:,.2f}">***</span></p></div>
        <div class="summary-item"><h3>净资产 (CNY)</h3><p><span class="sensitive-data" data-value="{context['net_worth']:,.2f}">***</span></p></div>
        <div class="summary-item"><h3>今日盈亏 (CNY)</h3><p class="{pnl_class_total}">{pnl_arrow} {abs(context['total_pnl_cny']):,.2f}</p><p class="pnl-details {pnl_class_total}">({pnl_arrow} {abs(context['total_pnl_percent']):.2f}%)</p></div>
    </div>
    <div class="card"><h2>持仓详情</h2><table><thead><tr><th>股票名称</th><th>代码</th><th>持股</th><th>当前价</th><th>市值 (CNY)</th><th>今日盈亏 (CNY)</th><th>涨跌幅</th></tr></thead><tbody>{''.join(portfolio_rows_html)}</tbody></table></div>
    <div class="card news-section"><h2>相关要闻</h2>{news_html if news_html else "<p>暂无相关新闻。</p>"}</div>
    """

def render_full_page_html(context):
    """Renders the complete HTML page, including the main content."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    main_content_html = render_main_content_html(context)
    # Safely escape quotes for the data attribute
    portfolio_json_str = json.dumps(context['portfolio']).replace("'", "&apos;").replace('"', "&quot;")

    return f"""
    <!DOCTYPE html><html lang="zh-CN"><head>
        <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>个人资产报告</title>
        <style>
            body {{ font-family: 'Noto Sans SC', sans-serif; margin: 0; background-color: #f4f7f9; color: #333; }}
            .container {{ max-width: 900px; margin: 30px auto; padding: 0 20px; }} .card {{ background-color: #fff; border-radius: 12px; box-shadow: 0 6px 20px rgba(0,0,0,0.07); padding: 25px; margin-bottom: 25px; }}
            .header {{ display: flex; justify-content: center; align-items: center; }} h1 {{ font-size: 28px; margin: 0; }}
            .controls {{ margin-left: 15px; display: flex; align-items: center; gap: 10px; }}
            .control-btn {{ cursor: pointer; color: #555; background: #f0f0f0; border-radius: 50%; width: 36px; height: 36px; display: flex; justify-content: center; align-items: center; transition: background-color 0.2s; }}
            .control-btn:hover {{ background-color: #e0e0e0; }}
            .report-time {{ text-align: center; color: #888; margin: 10px 0 25px; }}
            .summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 20px; }}
            .summary-item {{ background-color: #f8f9fa; text-align: center; padding: 20px; border-radius: 10px; }}
            .summary-item h3 {{ margin: 0 0 10px 0; font-size: 16px; color: #555; }} .summary-item p {{ margin: 0; font-size: 26px; font-weight: 700; }}
            .pnl-details {{ font-size: 18px; font-weight: normal; margin-top: 5px; }} .pnl-positive {{ color: #d04a4a; }} .pnl-negative {{ color: #47a27c; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }} th, td {{ padding: 14px; text-align: left; border-bottom: 1px solid #f0f0f0; }}
            .news-section h3 {{ margin-top: 20px; }} .news-section ul {{ list-style: none; padding-left: 0; }} .news-section li {{ padding: 10px 0; border-bottom: 1px solid #f0f0f0; }}
            .news-section a {{ text-decoration: none; color: #0056b3; }} .news-date {{ float: right; color: #999; font-size: 14px; }}
            
            /* Modal Styles */
            .modal {{ position: fixed; z-index: 100; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.5); display: none; justify-content: center; align-items: center; }}
            .modal-content {{ background-color: #fefefe; padding: 20px 30px; border-radius: 10px; width: 90%; max-width: 600px; box-shadow: 0 5px 15px rgba(0,0,0,0.3); }}
            .modal-header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #e5e5e5; padding-bottom: 15px; margin-bottom: 20px; }}
            .modal-header h2 {{ margin: 0; font-size: 22px; }}
            .close-btn {{ color: #aaa; font-size: 28px; font-weight: bold; cursor: pointer; }} .close-btn:hover {{ color: #000; }}
            .form-group {{ margin-bottom: 20px; }} .form-group label {{ display: block; margin-bottom: 8px; font-weight: 600; color: #333; }}
            .form-group input {{ width: 100%; padding: 10px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 6px; font-size: 16px; }}
            #stock-list-header {{ display: grid; grid-template-columns: 2fr 2fr 1fr auto; gap: 10px; font-weight: 600; color: #555; padding: 0 10px 5px; border-bottom: 2px solid #eee; margin-bottom: 10px; }}
            #stock-list .stock-item {{ display: grid; grid-template-columns: 2fr 2fr 1fr auto; gap: 10px; margin-bottom: 10px; align-items: center; }}
            .stock-item input {{ padding: 8px; border: 1px solid #ddd; border-radius: 4px; }}
            .delete-stock-btn {{ background-color: #e74c3c; color: white; border: none; width: 32px; height: 32px; border-radius: 50%; cursor: pointer; font-size: 16px; line-height: 32px; text-align: center; }}
            .modal-footer {{ text-align: right; border-top: 1px solid #e5e5e5; padding-top: 20px; margin-top: 20px; }}
            #add-stock-btn, #save-btn {{ background-color: #3498db; color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: 600; }}
            #add-stock-btn {{ background-color: #2ecc71; float: left; }}
            #loader {{ text-align: center; display: none; margin-top: 15px; }}
        </style>
    </head><body>
        <div id="portfolio-data" data-portfolio='{portfolio_json_str}'></div>
        <div class="container">
            <div class="header"><h1>个人资产报告</h1><div class="controls">
                <span id="edit-btn" class="control-btn" title="编辑"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34a.9959.9959 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg></span>
                <span id="visibility-toggle" class="control-btn" title="切换可见性"><svg id="eye-open" style="display:none;" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4.5C7 4.5 2.73 7.61 1 12c1.73 4.39 6 7.5 11 7.5s9.27-3.11 11-7.5C3.27 7.61 7 4.5 12 4.5zm0 10c-2.76 0-5-2.24-5-5s2.24-5 5-5 5 2.24 5 5-2.24 5-5 5zm0-8c-1.66 0-3 1.34-3 3s1.34 3 3 3 3-1.34 3-3-1.34-3-3z"/></svg><svg id="eye-closed" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 7c2.76 0 5 2.24 5 5 0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.43-4.75C21.27 7.61 17 4.5 12 4.5c-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65 0 1.66 1.34 3 3 3 .22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53-2.76 0-5-2.24-5-5 0-.79.2-1.53.53-2.2zm4.31-.78l3.15 3.15.02-.16c0-1.66-1.34-3-3-3l-.17.01z"/></svg></span>
            </div></div>
            <p class="report-time">报告生成时间: {now}</p>
            <div id="main-content">{main_content_html}</div>
        </div>
        <div id="edit-modal" class="modal"><div class="modal-content">
            <div class="modal-header"><h2>编辑持仓与负债</h2><span class="close-btn">&times;</span></div>
            <div class="form-group"><label for="liabilities-input">总负债 (CNY)</label><input type="number" id="liabilities-input" value="{context['liabilities']}"></div>
            <div class="form-group">
                <label>持仓股票</label>
                <div id="stock-list-header"><div>代码</div><div>名称</div><div>数量</div></div>
                <div id="stock-list"></div>
            </div>
            <div id="loader"><p>正在保存...</p></div>
            <div class="modal-footer">
                <button id="add-stock-btn">增加股票</button>
                <button id="save-btn">保存更改</button>
            </div>
        </div></div>
        <script>
            document.addEventListener('DOMContentLoaded', function() {{
                const dataEl = document.getElementById('portfolio-data');
                var portfolioData = JSON.parse(dataEl.dataset.portfolio.replace(/&quot;/g, '"').replace(/&apos;/g, "'"));
                
                const modal = document.getElementById('edit-modal');
                const mainContent = document.getElementById('main-content');
                let isDataVisible = false;

                function setupControls() {{
                    document.getElementById('edit-btn').addEventListener('click', () => {{
                        const stockList = document.getElementById('stock-list');
                        stockList.innerHTML = '';
                        for (const [code, details] of Object.entries(portfolioData)) {{
                            createStockItem(code, details.name, details.shares);
                        }}
                        modal.style.display = 'flex';
                    }});
                    document.getElementById('visibility-toggle').addEventListener('click', toggleVisibility);
                }}

                function createStockItem(code = '', name = '', shares = '') {{
                    const stockList = document.getElementById('stock-list');
                    const div = document.createElement('div');
                    div.className = 'stock-item';
                    div.innerHTML = `
                        <input placeholder="e.g., 002594.SZ" value="${{code}}">
                        <input placeholder="e.g., 比亚迪" value="${{name}}">
                        <input type="number" placeholder="e.g., 10000" value="${{shares}}">
                        <button class="delete-stock-btn">&times;</button>`;
                    div.querySelector('.delete-stock-btn').addEventListener('click', () => div.remove());
                    stockList.appendChild(div);
                }}

                function toggleVisibility() {{
                    isDataVisible = !isDataVisible;
                    document.querySelectorAll('.sensitive-data').forEach(el => {{
                        el.textContent = isDataVisible ? el.dataset.value : '***';
                    }});
                    document.getElementById('eye-open').style.display = isDataVisible ? 'block' : 'none';
                    document.getElementById('eye-closed').style.display = isDataVisible ? 'none' : 'block';
                }}
                
                document.getElementById('add-stock-btn').addEventListener('click', () => createStockItem());
                modal.querySelector('.close-btn').addEventListener('click', () => modal.style.display = 'none');

                document.getElementById('save-btn').addEventListener('click', async () => {{
                    const newPortfolio = {{}};
                    document.querySelectorAll('#stock-list .stock-item').forEach(item => {{
                        const inputs = item.querySelectorAll('input');
                        if (inputs.length === 3 && inputs[0].value) {{
                            newPortfolio[inputs[0].value.trim()] = {{ name: inputs[1].value.trim(), shares: parseInt(inputs[2].value, 10) }};
                        }}
                    }});
                    const newLiabilities = parseFloat(document.getElementById('liabilities-input').value);

                    const loader = document.getElementById('loader');
                    loader.style.display = 'block';

                    try {{
                        const response = await fetch('/api/update', {{
                            method: 'POST',
                            headers: {{ 'Content-Type': 'application/json' }},
                            body: JSON.stringify({{ portfolio: newPortfolio, liabilities: newLiabilities }})
                        }});
                        const result = await response.json();
                        if (!response.ok) throw new Error(result.error || '保存失败');
                        
                        mainContent.innerHTML = result.html;
                        portfolioData = result.portfolio;
                        const newDataEl = document.getElementById('portfolio-data');
                        newDataEl.dataset.portfolio = JSON.stringify(result.portfolio).replace(/'/g, "&apos;").replace(/"/g, "&quot;");

                        //toggleVisibility(); // Re-apply visibility state
                        modal.style.display = 'none';
                    }} catch (error) {{
                        alert('保存时出错: ' + error.message);
                    }} finally {{
                        loader.style.display = 'none';
                    }}
                }});
                
                modal.addEventListener('click', (e) => {{ if (e.target === modal) modal.style.display = 'none'; }});
                setupControls();
                //toggleVisibility();
            }});
        </script>
    </body></html>"""

# --- Flask Routes ---
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def show_report(path):
    if not r:
        return Response("<h1>错误: Redis 未配置</h1><p>请检查服务器环境变量 KV_REDIS_URL。</p>", status=500)

    # Read the single config object from Redis
    config_json = r.get('asset_config')

    # If the object doesn't exist, use defaults and save it
    if config_json is None:
        print("未在Redis中找到 'asset_config'，正在使用默认值并创建...")
        config = {"portfolio": DEFAULT_PORTFOLIO, "liabilities": DEFAULT_LIABILITIES_CNY}
        try:
            r.set('asset_config', json.dumps(config))
        except Exception as e:
            print(f"创建默认配置失败: {e}")
    else:
        config = json.loads(config_json)

    context = get_report_context(config.get('portfolio', {}), config.get('liabilities', 0))
    html_content = render_full_page_html(context)
    
    response = Response(html_content, mimetype='text/html')
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return response

@app.route('/api/update', methods=['POST'])
def update_portfolio():
    if not r:
        return jsonify({'error': 'Redis not configured on server.'}), 500
    try:
        data = request.get_json()
        if 'portfolio' not in data or 'liabilities' not in data:
            return jsonify({'error': 'Invalid data format.'}), 400
        
        new_config = {"portfolio": data['portfolio'], "liabilities": data['liabilities']}
        r.set('asset_config', json.dumps(new_config))
        
        # Immediately get fresh data and render the HTML snippet
        context = get_report_context(new_config['portfolio'], new_config['liabilities'])
        html_snippet = render_main_content_html(context)
        
        return jsonify({
            'status': 'success', 
            'html': html_snippet,
            'portfolio': new_config['portfolio']
        })
    except Exception as e:
        print(f"Error updating Redis: {e}")
        return jsonify({'error': 'An internal error occurred.'}), 500
