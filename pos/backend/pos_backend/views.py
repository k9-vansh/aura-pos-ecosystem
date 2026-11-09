import os
from django.http import HttpResponse

def root_portal_view(request):
    host = request.get_host().lower()
    is_local = host.startswith('localhost') or host.startswith('127.0.0.1')
    
    frontend_base = os.getenv('FRONTEND_URL', 'https://aura-pos-ecosystem-distecosystem.vercel.app').rstrip('/')
    
    till_url = 'http://localhost:4200/' if is_local else f'{frontend_base}/'
    owner_url = 'http://localhost:5190/' if is_local else f'{frontend_base}/owner/'
    staff_url = 'http://localhost:5210/' if is_local else f'{frontend_base}/staff/'
    client_url = 'http://localhost:5200/' if is_local else f'{frontend_base}/client/'
    
    status_text = "Core POS Backend API Gateway Active (Port 8001)" if is_local else "Core POS Cloud Backend & API Gateway Active"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Aura POS — Ecosystem Hub & API Gateway</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Playfair+Display:wght@600;700&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      background: radial-gradient(circle at 50% 0%, #1e293b 0%, #0f172a 100%);
      color: #f8fafc;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 20px;
    }}
    .container {{
      width: 100%;
      max-width: 1040px;
    }}
    .header {{
      text-align: center;
      margin-bottom: 40px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.3);
      color: #34d399;
      font-size: 13px;
      font-weight: 600;
      border-radius: 9999px;
      margin-bottom: 16px;
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      background: #10b981;
      border-radius: 50%;
      box-shadow: 0 0 10px #10b981;
    }}
    h1 {{
      font-family: 'Playfair Display', serif;
      font-size: 38px;
      font-weight: 700;
      letter-spacing: 0.04em;
      color: #ffffff;
      margin-bottom: 8px;
    }}
    .subtitle {{
      color: #94a3b8;
      font-size: 15px;
      max-width: 620px;
      margin: 0 auto;
      line-height: 1.6;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(310px, 1fr));
      gap: 20px;
      margin-bottom: 30px;
    }}
    .card {{
      background: rgba(30, 41, 59, 0.7);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 18px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: all 0.25s ease;
      text-decoration: none;
      color: inherit;
      position: relative;
      overflow: hidden;
    }}
    .card:hover {{
      transform: translateY(-4px);
      border-color: rgba(212, 175, 55, 0.4);
      box-shadow: 0 16px 32px -8px rgba(0, 0, 0, 0.5);
    }}
    .card-top {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      margin-bottom: 14px;
    }}
    .card-icon {{
      font-size: 26px;
      width: 48px;
      height: 48px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(255, 255, 255, 0.05);
      border-radius: 12px;
      border: 1px solid rgba(255, 255, 255, 0.1);
    }}
    .card-tag {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      padding: 4px 10px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.08);
      color: #cbd5e1;
    }}
    .card-title {{
      font-size: 19px;
      font-weight: 700;
      color: #f1f5f9;
      margin-bottom: 6px;
    }}
    .card-desc {{
      font-size: 13px;
      color: #94a3b8;
      line-height: 1.5;
      margin-bottom: 18px;
    }}
    .card-footer {{
      border-top: 1px solid rgba(255, 255, 255, 0.06);
      padding-top: 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 13px;
    }}
    .credentials {{
      font-size: 12px;
      color: #d4af37;
      font-weight: 600;
      font-family: monospace;
      background: rgba(212, 175, 55, 0.1);
      padding: 4px 8px;
      border-radius: 6px;
    }}
    .btn-launch {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: #38bdf8;
      font-weight: 600;
      font-size: 13px;
    }}
    .admin-bar {{
      background: rgba(30, 41, 59, 0.5);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 14px;
      padding: 16px 24px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .admin-bar-left {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 14px;
      color: #cbd5e1;
    }}
    .admin-btn {{
      background: #334155;
      color: #f8fafc;
      padding: 8px 16px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      text-decoration: none;
      transition: background 0.2s;
    }}
    .admin-btn:hover {{
      background: #475569;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <div class="badge">
        <span class="pulse-dot"></span>
        {status_text}
      </div>
      <h1>AURA SALON &amp; SPA</h1>
      <p class="subtitle">
        Enterprise multi-location Point-of-Sale &amp; operations suite.
        Select any frontend portal below to launch the application.
      </p>
    </header>

    <div class="grid">
      <!-- POS Till -->
      <a href="{till_url}" target="_blank" class="card">
        <div>
          <div class="card-top">
            <div class="card-icon">&#128187;</div>
            <span class="card-tag">Angular 18 App</span>
          </div>
          <div class="card-title">POS Till &amp; Back-Office</div>
          <div class="card-desc">High-speed register checkout, appointment calendar grid, BOM consumable tracking, and RBAC permissions.</div>
        </div>
        <div class="card-footer">
          <span class="credentials">admin@aurasalon.io / admin123</span>
          <span class="btn-launch">Open App &rarr;</span>
        </div>
      </a>

      <!-- Owner App -->
      <a href="{owner_url}" target="_blank" class="card">
        <div>
          <div class="card-top">
            <div class="card-icon">&#128081;</div>
            <span class="card-tag">React + Vite</span>
          </div>
          <div class="card-title">Owner Analytics Dashboard</div>
          <div class="card-desc">Real-time revenue metrics, cross-salon benchmarking, stylist leaderboards, and daily master PIN access.</div>
        </div>
        <div class="card-footer">
          <span class="credentials">9811122233 / admin123</span>
          <span class="btn-launch">Open App &rarr;</span>
        </div>
      </a>

      <!-- Staff App -->
      <a href="{staff_url}" target="_blank" class="card">
        <div>
          <div class="card-top">
            <div class="card-icon">&#128241;</div>
            <span class="card-tag">React + Vite</span>
          </div>
          <div class="card-title">Staff Mobile Portal</div>
          <div class="card-desc">Individual digital job card, scheduled client visits, commission breakdown, and personal attendance tracking.</div>
        </div>
        <div class="card-footer">
          <span class="credentials">9000100001 / PIN: 1234</span>
          <span class="btn-launch">Open App &rarr;</span>
        </div>
      </a>

      <!-- Client App -->
      <a href="{client_url}" target="_blank" class="card">
        <div>
          <div class="card-top">
            <div class="card-icon">&#127915;</div>
            <span class="card-tag">React + Vite</span>
          </div>
          <div class="card-title">Client Self-Service App</div>
          <div class="card-desc">Client wallet (advances, cashback, vouchers), appointment booking, and itemized invoice downloads.</div>
        </div>
        <div class="card-footer">
          <span class="credentials">9876543210 / PIN: 1234</span>
          <span class="btn-launch">Open App &rarr;</span>
        </div>
      </a>
    </div>

    <div class="admin-bar">
      <div class="admin-bar-left">
        <span>&#9881; <strong>Django Administration:</strong> Manage raw database models, permissions, and audit logs.</span>
      </div>
      <a href="/admin/" target="_blank" class="admin-btn">Open Django Admin Panel</a>
    </div>
  </div>
</body>
</html>
"""
    return HttpResponse(html_content, content_type='text/html; charset=utf-8')
