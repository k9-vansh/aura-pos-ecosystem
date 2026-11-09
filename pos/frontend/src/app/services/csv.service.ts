import { Injectable } from '@angular/core';

@Injectable({
  providedIn: 'root'
})
export class CsvService {
  exportToCsv(filename: string, headers: string[], rows: any[][]) {
    // Create CSV content with UTF-8 BOM so Excel opens it correctly
    let csvContent = "\uFEFF" + headers.map(h => `"${h}"`).join(",") + "\n";
    
    if (rows && rows.length) {
    
    rows.forEach(row => {
      const rowData = row.map(cell => {
        if (cell === null || cell === undefined) return '""';
        const str = String(cell).replace(/"/g, '""'); // escape double quotes
        return `"${str}"`; // wrap in quotes
      });
      csvContent += rowData.join(",") + "\n";
    });
    }
    
    // Download Blob
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename.endsWith('.csv') ? filename : `${filename}.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
}
