import sys
with open('frontend/src/app/centers/centers.html', 'r', encoding='utf-8') as f:
    html = f.read()

dropdown = """
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Branch (SAP)</label>
            <select
              [(ngModel)]="newCenter.sap_branch_name"
              name="sap_branch_name"
              class="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-1 focus:ring-indigo-500 focus:border-indigo-500"
            >
              <option value="">-- No Branch --</option>
              <option *ngFor="let b of sapBranches" [value]="b.BPLName">
                {{ b.BPLName }}
              </option>
            </select>
          </div>
"""

# Find Region input and inject this right after
target = """          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">Region</label>
            <input
              type="text"
              [(ngModel)]="newCenter.region"
              name="region"
              class="w-full px-3 py-2 border border-gray-300 rounded-md shadow-sm focus:outline-none focus:ring-1 focus:ring-indigo-500 focus:border-indigo-500"
              placeholder="e.g. North"
            />
          </div>"""

if target in html:
    html = html.replace(target, target + dropdown)
    with open('frontend/src/app/centers/centers.html', 'w', encoding='utf-8') as f:
        f.write(html)
    print("Injected dropdown successfully!")
else:
    print("Could not find Region input block!")
