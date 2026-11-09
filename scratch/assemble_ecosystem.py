import os
import shutil

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out_dir = os.path.join(root_dir, 'dist_ecosystem')

if os.path.exists(out_dir):
    shutil.rmtree(out_dir)
os.makedirs(out_dir)

# 1. Copy pos/frontend build to root
pos_browser = os.path.join(root_dir, 'pos', 'frontend', 'dist', 'pos-frontend', 'browser')
if os.path.exists(pos_browser):
    shutil.copytree(pos_browser, out_dir, dirs_exist_ok=True)
    print('Copied POS Frontend (Angular) to dist_ecosystem root')

# 2. Copy owner-app to dist_ecosystem/owner
owner_dist = os.path.join(root_dir, 'owner-app', 'dist')
if os.path.exists(owner_dist):
    shutil.copytree(owner_dist, os.path.join(out_dir, 'owner'))
    print('Copied Owner App to dist_ecosystem/owner')

# 3. Copy staff-app to dist_ecosystem/staff
staff_dist = os.path.join(root_dir, 'staff-app', 'dist')
if os.path.exists(staff_dist):
    shutil.copytree(staff_dist, os.path.join(out_dir, 'staff'))
    print('Copied Staff App to dist_ecosystem/staff')

# 4. Copy clients-app to dist_ecosystem/client
client_dist = os.path.join(root_dir, 'clients-app', 'dist')
if os.path.exists(client_dist):
    shutil.copytree(client_dist, os.path.join(out_dir, 'client'))
    print('Copied Client App to dist_ecosystem/client')

# 5. Create vercel.json
vercel_json = """{
  "version": 2,
  "cleanUrls": true,
  "rewrites": [
    { "source": "/owner/api/(.*)", "destination": "https://aura-pos-backend.onrender.com/owner/api/$1" },
    { "source": "/team/api/(.*)", "destination": "https://aura-pos-backend.onrender.com/team/api/$1" },
    { "source": "/client/api/(.*)", "destination": "https://aura-pos-backend.onrender.com/client/api/$1" },
    { "source": "/staff/api/(.*)", "destination": "https://aura-pos-backend.onrender.com/staff/$1" },
    { "source": "/accounts/(.*)", "destination": "https://aura-pos-backend.onrender.com/accounts/$1" },
    { "source": "/billing/(.*)", "destination": "https://aura-pos-backend.onrender.com/billing/$1" },
    { "source": "/finance/(.*)", "destination": "https://aura-pos-backend.onrender.com/finance/$1" },
    { "source": "/inventory/(.*)", "destination": "https://aura-pos-backend.onrender.com/inventory/$1" },
    { "source": "/appointments/(.*)", "destination": "https://aura-pos-backend.onrender.com/appointments/$1" },
    { "source": "/clients/(.*)", "destination": "https://aura-pos-backend.onrender.com/clients/$1" },
    { "source": "/services/(.*)", "destination": "https://aura-pos-backend.onrender.com/services/$1" },
    { "source": "/marketing/(.*)", "destination": "https://aura-pos-backend.onrender.com/marketing/$1" },
    { "source": "/salon_admin/(.*)", "destination": "https://aura-pos-backend.onrender.com/salon_admin/$1" },
    { "source": "/approvals/(.*)", "destination": "https://aura-pos-backend.onrender.com/approvals/$1" },
    { "source": "/audit_logs/(.*)", "destination": "https://aura-pos-backend.onrender.com/audit_logs/$1" },
    { "source": "/notifications/(.*)", "destination": "https://aura-pos-backend.onrender.com/notifications/$1" },
    { "source": "/app-settings/(.*)", "destination": "https://aura-pos-backend.onrender.com/app-settings/$1" },
    { "source": "/sap-sync/(.*)", "destination": "https://aura-pos-backend.onrender.com/sap-sync/$1" },
    { "source": "/admin/(.*)", "destination": "https://aura-pos-backend.onrender.com/admin/$1" },
    { "source": "/media/(.*)", "destination": "https://aura-pos-backend.onrender.com/media/$1" },
    { "source": "/owner/(.*)", "destination": "/owner/$1" },
    { "source": "/owner", "destination": "/owner/index.html" },
    { "source": "/staff/(.*)", "destination": "/staff/$1" },
    { "source": "/staff", "destination": "/staff/index.html" },
    { "source": "/client/(.*)", "destination": "/client/$1" },
    { "source": "/client", "destination": "/client/index.html" },
    { "source": "/(.*)", "destination": "/index.html" }
  ]
}"""

with open(os.path.join(out_dir, 'vercel.json'), 'w', encoding='utf-8') as f:
    f.write(vercel_json)

with open(os.path.join(root_dir, 'vercel.json'), 'w', encoding='utf-8') as f:
    f.write(vercel_json)

print('Generated ecosystem and vercel.json successfully!')
