import sqlite3, os
db = os.path.join('backend', 'db.sqlite3')
conn = sqlite3.connect(db)
c = conn.cursor()
critical = ['billing_invoice', 'staff_staffmember', 'clients_client', 'appointments_appointment', 'billing_invoiceitem', 'finance_daybook']
for t in critical:
    c.execute('SELECT count(*) FROM ' + t)
    count = c.fetchone()[0]
    c.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (t,))
    idxs = [r[0] for r in c.fetchall()]
    print(t, '|', count, 'rows |', len(idxs), 'indexes:', idxs[:4])
conn.close()
