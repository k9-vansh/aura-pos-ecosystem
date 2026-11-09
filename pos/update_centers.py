import sys
with open('frontend/src/app/centers/centers.ts', 'r', encoding='utf-8') as f:
    code = f.read()

code = code.replace(
    "import { ApiService } from '../services/api';",
    "import { HttpClient } from '@angular/common/http';\nimport { ApiService } from '../services/api';"
)

code = code.replace(
    "address: '',\n    region: '',\n    phone: '',",
    "address: '',\n    region: '',\n    sap_branch_name: '',\n    phone: '',"
)

code = code.replace(
    "isOwner = false;\n  hasGlobalAccess = false;\n  permissions: any = {};\n  showInactive = true;",
    "isOwner = false;\n  hasGlobalAccess = false;\n  permissions: any = {};\n  showInactive = true;\n  sapBranches: any[] = [];\n  http = inject(HttpClient);"
)

code = code.replace(
    "}\n    this.loadCenters();\n  }",
    "}\n    this.loadCenters();\n    this.loadSapBranches();\n  }\n\n  loadSapBranches() {\n    this.http.get<any[]>('/api/sap-branches/').subscribe({\n      next: (data) => {\n        this.sapBranches = data;\n        this.cdr.detectChanges();\n      },\n      error: (err) => console.error('Failed to load SAP branches', err)\n    });\n  }"
)

with open('frontend/src/app/centers/centers.ts', 'w', encoding='utf-8') as f:
    f.write(code)
print('Done!')
