/**
 * Google Workspace (Drive & Sheets) API Integration Service
 * Uses OAuth Access Token obtained via client-side Firebase Auth
 */

export interface GoogleDriveFile {
  id: string;
  name: string;
  mimeType: string;
  webViewLink?: string;
  createdTime?: string;
  modifiedTime?: string;
  size?: string;
}

export interface SheetExportResult {
  spreadsheetId: string;
  spreadsheetUrl: string;
  title: string;
}

/**
 * List files from Google Drive (filtered for Spreadsheets or Blessing reports)
 */
export async function listGoogleDriveFiles(accessToken: string): Promise<GoogleDriveFile[]> {
  try {
    const q = encodeURIComponent("trashed = false and (mimeType = 'application/vnd.google-apps.spreadsheet' or name contains 'Blessing')");
    const fields = encodeURIComponent("files(id, name, mimeType, webViewLink, createdTime, modifiedTime, size)");
    const url = `https://www.googleapis.com/drive/v3/files?q=${q}&fields=${fields}&pageSize=20&orderBy=modifiedTime desc`;

    const response = await fetch(url, {
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    });

    if (!response.ok) {
      const errJson = await response.json().catch(() => ({}));
      throw new Error(errJson.error?.message || `Google Drive API error: ${response.statusText}`);
    }

    const data = await response.json();
    return data.files || [];
  } catch (error: any) {
    console.error('Failed to list Google Drive files:', error);
    throw error;
  }
}

/**
 * Delete a file in Google Drive
 * Requires explicit prior user confirmation per Google Workspace skill rules!
 */
export async function deleteDriveFile(accessToken: string, fileId: string): Promise<boolean> {
  try {
    const response = await fetch(`https://www.googleapis.com/drive/v3/files/${fileId}`, {
      method: 'DELETE',
      headers: {
        Authorization: `Bearer ${accessToken}`,
      },
    });

    if (!response.ok && response.status !== 204) {
      const errJson = await response.json().catch(() => ({}));
      throw new Error(errJson.error?.message || `Failed to delete file: ${response.statusText}`);
    }

    return true;
  } catch (error: any) {
    console.error('Failed to delete Google Drive file:', error);
    throw error;
  }
}

/**
 * Export active baskets and portfolio state into a newly created Google Spreadsheet
 */
export async function exportBasketsToGoogleSheet(
  accessToken: string,
  baskets: any[],
  portfolio: any
): Promise<SheetExportResult> {
  try {
    const dateStr = new Date().toISOString().replace(/T/, ' ').replace(/\..+/, '');
    const title = `Blessing AI — Quant Report (${dateStr})`;

    // 1. Create Spreadsheet
    const createRes = await fetch('https://sheets.googleapis.com/v4/spreadsheets', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        properties: {
          title,
        },
        sheets: [
          {
            properties: {
              title: 'Portfolio Summary',
              gridProperties: { rowCount: 50, columnCount: 12 },
            },
          },
          {
            properties: {
              title: 'Active Baskets',
              gridProperties: { rowCount: 100, columnCount: 12 },
            },
          },
        ],
      }),
    });

    if (!createRes.ok) {
      const errJson = await createRes.json().catch(() => ({}));
      throw new Error(errJson.error?.message || `Google Sheets creation failed: ${createRes.statusText}`);
    }

    const sheetData = await createRes.json();
    const spreadsheetId = sheetData.spreadsheetId;
    const spreadsheetUrl = sheetData.spreadsheetUrl;

    // 2. Populate Portfolio Summary tab
    const summaryValues = [
      ['BLESSING AI v0.2 — QUANTITATIVE PORTFOLIO AUDIT'],
      ['Export Timestamp (UTC)', new Date().toISOString()],
      ['Infrastructure Model', 'Google SaaS-First (Cloud SQL, Firebase, BigQuery)'],
      ['Target Exchange', 'Binance Global (Spot & USDⓈ-M Futures)'],
      ['Evidence Status', portfolio?.evidence_status ?? 'UNKNOWN / VERIFY WORKER SNAPSHOT'],
      [''],
      ['METRIC', 'VALUE', 'UNIT / STATUS'],
      ['Total Equity', portfolio?.equity ?? 'UNKNOWN', 'USDT'],
      ['Total Balance', portfolio?.balance ?? 'UNKNOWN', 'USDT'],
      ['Margin Utilization', portfolio?.margin_utilization ?? 'UNKNOWN', '%'],
      ['Effective Leverage', portfolio?.effective_leverage ?? 'UNKNOWN', 'x'],
      ['Current Drawdown', portfolio?.drawdown ?? 'UNKNOWN', '%'],
      ['Risk State', portfolio?.risk_state ?? 'UNKNOWN', 'Worker snapshot required'],
      ['Aggregate Long Exp', portfolio?.long_exposure ?? 'UNKNOWN', 'BTC / ETH'],
      ['Aggregate Short Exp', portfolio?.short_exposure ?? 'UNKNOWN', 'Hedge / Trend'],
      ['Active Baskets Count', baskets.length, 'Baskets'],
    ];

    await fetch(
      `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}/values/'Portfolio Summary'!A1:C${summaryValues.length}?valueInputOption=USER_ENTERED`,
      {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          range: `'Portfolio Summary'!A1:C${summaryValues.length}`,
          majorDimension: 'ROWS',
          values: summaryValues,
        }),
      }
    );

    // 3. Populate Active Baskets tab
    const basketHeaders = [
      'Basket ID',
      'Instrument',
      'Direction',
      'State',
      'Strategy',
      'Grid Depth',
      'Max Levels',
      'Total Size',
      'Avg Entry Price',
      'Current Mark',
      'Net PnL (USDT)',
      'Last Updated',
    ];

    const basketRows = baskets.map((b) => [
      b.basket_id || b.basketId,
      b.instrument,
      b.direction,
      b.state,
      b.strategy_id || 'STRUCTURAL_GRID',
      b.grid_depth ?? b.gridDepth ?? 0,
      b.max_grid_levels ?? b.maxGridLevels ?? 5,
      b.total_size ?? b.totalSize ?? 0,
      b.average_entry ?? b.averageEntry ?? 0,
      b.current_mark_price ?? 0,
      b.net_pnl ?? b.netPnl ?? 0,
      b.last_updated || new Date().toISOString(),
    ]);

    const allBasketValues = [basketHeaders, ...basketRows];

    await fetch(
      `https://sheets.googleapis.com/v4/spreadsheets/${spreadsheetId}/values/'Active Baskets'!A1:L${allBasketValues.length}?valueInputOption=USER_ENTERED`,
      {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          range: `'Active Baskets'!A1:L${allBasketValues.length}`,
          majorDimension: 'ROWS',
          values: allBasketValues,
        }),
      }
    );

    return {
      spreadsheetId,
      spreadsheetUrl,
      title,
    };
  } catch (error: any) {
    console.error('Failed to export to Google Sheets:', error);
    throw error;
  }
}
