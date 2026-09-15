import React, { useState } from 'react';
import {
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  Tooltip,
  Legend,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
} from 'recharts';
import {
  Wallet,
  TrendingUp,
  Scale,
  Coins,
  Bot,
  RefreshCw,
  ExternalLink,
  ArrowRight,
  PieChart as PieChartIcon,
  BarChart3,
  Layers,
  CheckCircle2,
  AlertTriangle,
  Maximize2,
  Minimize2,
  X,
  ShieldCheck,
  Search,
  ChevronRight,
  Database,
  Lock,
  Sparkles,
} from 'lucide-react';
import { AccountData, SubWalletSummary, TwoLayerAsset } from '../types';
import { apiClient } from '../api/client';

interface BalanceAllocationProps {
  account: AccountData;
  onAccountUpdated?: (newAccount: AccountData) => void;
  onNavigateTab?: (tab: 'cockpit' | 'wallet' | 'backtest' | 'copilot' | 'bigquery' | 'architecture') => void;
  onOpenKeyModal?: () => void;
  isModal?: boolean;
  onCloseModal?: () => void;
}

const CATEGORY_CONFIG: Record<
  string,
  {
    name: string;
    color: string;
    bgColor: string;
    borderColor: string;
    icon: React.ReactNode;
    binanceUrl: string;
    description: string;
  }
> = {
  TRADING_BOT: {
    name: 'Trading Bot',
    color: '#06b6d4', // cyan-500
    bgColor: 'bg-cyan-950/40',
    borderColor: 'border-cyan-800/60',
    icon: <Bot className="w-4 h-4 text-cyan-400" />,
    binanceUrl: 'https://www.binance.com/en/trading-bots',
    description: 'ทุนสำรองในคำสั่ง Grid และ กลยุทธ์ Alpha Engines (Strategy Bots)',
  },
  PORTFOLIO_MARGIN: {
    name: 'Portfolio Margin (PM)',
    color: '#6366f1', // indigo-500
    bgColor: 'bg-indigo-950/40',
    borderColor: 'border-indigo-800/60',
    icon: <Scale className="w-4 h-4 text-indigo-400" />,
    binanceUrl: 'https://www.binance.com/en/my/wallet/account/portfolio-margin',
    description: 'หลักประกัน Cross Margin สำหรับคุม Leverage, Futures & Hedges',
  },
  EARN: {
    name: 'Simple Earn',
    color: '#f59e0b', // amber-500
    bgColor: 'bg-amber-950/40',
    borderColor: 'border-amber-800/60',
    icon: <Coins className="w-4 h-4 text-amber-400" />,
    binanceUrl: 'https://www.binance.com/en/earn',
    description: 'สินทรัพย์ใน Flexible Earn & Locked Staking รับผลตอบแทนดอกเบี้ย APR',
  },
  SPOT: {
    name: 'Spot Wallet',
    color: '#10b981', // emerald-500
    bgColor: 'bg-emerald-950/40',
    borderColor: 'border-emerald-800/60',
    icon: <Wallet className="w-4 h-4 text-emerald-400" />,
    binanceUrl: 'https://www.binance.com/en/my/wallet/account/main',
    description: 'เงินสดคงเหลือในกระเป๋า Spot พร้อมส่งคำสั่งซื้อขายได้ทันที',
  },
  FUNDING: {
    name: 'Funding Wallet',
    color: '#a855f7', // purple-500
    bgColor: 'bg-purple-950/40',
    borderColor: 'border-purple-800/60',
    icon: <Wallet className="w-4 h-4 text-purple-400" />,
    binanceUrl: 'https://www.binance.com/en/my/wallet/funding',
    description: 'กระเป๋าสำหรับ P2P, Binance Pay และการโอนย้ายบุคคลภายนอก',
  },
};

export const BalanceAllocation: React.FC<BalanceAllocationProps> = ({
  account,
  onAccountUpdated,
  onNavigateTab,
  onOpenKeyModal,
  isModal = false,
  onCloseModal,
}) => {
  const [chartType, setChartType] = useState<'PIE' | 'BAR'>('PIE');
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null);
  const [isSyncing, setIsSyncing] = useState<boolean>(false);
  const [searchFilter, setSearchFilter] = useState<string>('');
  const [syncFeedback, setSyncFeedback] = useState<{
    type: 'success' | 'warn' | 'error';
    msg: string;
  } | null>(null);

  const equity = account?.equity ?? 0;
  const rawSubWallets = account?.sub_wallets ?? [];
  const rawTwoLayerAssets = account?.two_layer_assets ?? [];
  const lastSyncTime = account?.last_sync_time;
  const source = account?.source ?? 'SIMULATED';
  const hasVerifiedSnapshot = account?.verified === true && (source === 'BINANCE_TESTNET' || source === 'BINANCE_MAINNET');
  const snapshotLabel =
    source === 'BINANCE_TESTNET' || source === 'BINANCE_MAINNET'
      ? hasVerifiedSnapshot ? source.replace('_', ' ') : `${source.replace('_', ' ')} / UNVERIFIED`
      : 'NO VERIFIED BINANCE SNAPSHOT';
  const subWallets: SubWalletSummary[] = rawSubWallets;
  const twoLayerAssets: TwoLayerAsset[] = rawTwoLayerAssets;

  // Prepare Pie Chart Data from SubWallets
  const pieChartData = subWallets
    .filter((w) => w.usdVal > 0.01)
    .map((w) => {
      const cfg = CATEGORY_CONFIG[w.category] || CATEGORY_CONFIG.SPOT;
      return {
        name: cfg.name,
        category: w.category,
        value: Number(w.usdVal.toFixed(2)),
        pct: w.pctOfTotal,
        color: cfg.color,
        btcVal: w.btcVal,
      };
    });

  // Prepare Bar Chart Data (Distribution across assets and categories)
  const barChartData = twoLayerAssets.map((assetItem) => {
    const row: any = {
      asset: assetItem.asset,
      total: Number(assetItem.totalUsdVal.toFixed(2)),
      TRADING_BOT: 0,
      PORTFOLIO_MARGIN: 0,
      EARN: 0,
      SPOT: 0,
    };
    assetItem.allocations.forEach((al) => {
      if (row[al.category] !== undefined) {
        row[al.category] = Number((row[al.category] + al.usdVal).toFixed(2));
      }
    });
    return row;
  });

  const formatSnapshotCurrency = (value: number, digits = 2) =>
    hasVerifiedSnapshot
      ? `$${value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`
      : 'UNKNOWN';
  const formatSnapshotNumber = (value: number, digits = 2) =>
    hasVerifiedSnapshot ? value.toFixed(digits) : 'UNKNOWN';

  const handleSyncBinance = async () => {
    setIsSyncing(true);
    setSyncFeedback(null);
    try {
      const data = await apiClient.post<{
        success: boolean;
        account?: AccountData;
        message?: string;
      }>('/api/binance/sync-account', {});
      if (data.success && data.account) {
        if (onAccountUpdated) {
          onAccountUpdated(data.account);
        }
        setSyncFeedback({
          type: 'success',
          msg: `ซิงค์ยอดเงินจริงสำเร็จ! ยอดรวมพอร์ต $${data.account.equity.toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}`,
        });
      } else {
        setSyncFeedback({
          type: 'warn',
          msg: data.message || 'ไม่สามารถดึงยอดเงินจาก Binance API ได้ ตรวจสอบ Key หรือ Network',
        });
      }
    } catch (err: any) {
      setSyncFeedback({
        type: 'error',
        msg: `เกิดข้อผิดพลาดในการเชื่อมต่อ Binance: ${err.message}`,
      });
    } finally {
      setIsSyncing(false);
      setTimeout(() => {
        setSyncFeedback((prev) => (prev?.type === 'success' ? null : prev));
      }, 7000);
    }
  };

  // Filtered Assets based on selection & search
  const filteredAssets = twoLayerAssets.filter((item) => {
    const matchesSearch = item.asset.toLowerCase().includes(searchFilter.toLowerCase());
    if (!matchesSearch) return false;
    if (!selectedCategory) return true;
    return item.allocations.some((al) => al.category === selectedCategory && al.usdVal > 0);
  });

  const CustomPieTooltip = ({ active, payload }: any) => {
    if (active && payload && payload.length) {
      const data = payload[0].payload;
      return (
        <div className="bg-zinc-950 border border-zinc-700 p-3 rounded-lg shadow-xl text-xs space-y-1 z-50">
          <div className="flex items-center space-x-2 font-bold text-zinc-100">
            <span
              className="w-2.5 h-2.5 rounded-full"
              style={{ backgroundColor: data.color }}
            />
            <span>{data.name}</span>
          </div>
          <div className="text-emerald-400 font-mono text-sm font-semibold">
            ${data.value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <div className="text-zinc-400 text-[11px] flex items-center justify-between space-x-3">
            <span>สัดส่วนในพอร์ต:</span>
            <strong className="text-cyan-300 font-mono">{data.pct}%</strong>
          </div>
          {data.btcVal > 0 && (
            <div className="text-zinc-400 text-[11px] flex items-center justify-between space-x-3">
              <span>มูลค่าเทียบเคียง:</span>
              <span className="text-zinc-300 font-mono">≈ {data.btcVal.toFixed(6)} BTC</span>
            </div>
          )}
        </div>
      );
    }
    return null;
  };

  const CustomBarTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      return (
        <div className="bg-zinc-950 border border-zinc-700 p-3 rounded-lg shadow-xl text-xs space-y-1.5 z-50 min-w-[180px]">
          <div className="font-bold text-zinc-100 border-b border-zinc-800 pb-1 flex items-center justify-between">
            <span>{label}</span>
            <span className="text-zinc-400 text-[10px]">การจัดสรรตามกระเป๋า</span>
          </div>
          {payload.map((entry: any, index: number) => {
            if (entry.value <= 0) return null;
            const cfg = CATEGORY_CONFIG[entry.dataKey] || { name: entry.name, color: entry.fill };
            return (
              <div key={index} className="flex items-center justify-between text-[11px]">
                <span className="flex items-center space-x-1 text-zinc-300">
                  <span className="w-2 h-2 rounded-full" style={{ backgroundColor: entry.color }} />
                  <span>{cfg.name}:</span>
                </span>
                <span className="font-mono font-semibold text-zinc-100">
                  ${entry.value.toFixed(2)}
                </span>
              </div>
            );
          })}
        </div>
      );
    }
    return null;
  };

  return (
    <div
      className={`space-y-6 ${
        isModal ? 'p-1' : ''
      }`}
      id="balance-allocation-component"
    >
      {/* 1. Header Toolbar */}
      <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl p-4 flex flex-wrap items-center justify-between gap-4 shadow-sm">
        <div className="flex items-center space-x-3">
          <div className="p-2.5 rounded-xl bg-cyan-950/80 border border-cyan-800/70 text-cyan-400 shadow-sm">
            <PieChartIcon className="w-6 h-6" />
          </div>
          <div>
            <div className="flex items-center space-x-2">
              <h1 className="text-lg font-bold text-zinc-100">
                การจัดสรรเงินและกระเป๋า (Balance Allocation & Sub-Wallets)
              </h1>
              <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-cyan-950 text-cyan-300 border border-cyan-800/60">
                {snapshotLabel}
              </span>
            </div>
            <p className="text-xs text-zinc-400 mt-0.5">
              แสดงการกระจายตัวของสินทรัพย์ระหว่าง <strong>Trading Bot</strong>, <strong>Portfolio Margin</strong>, <strong>Simple Earn</strong>, และ <strong>Spot Wallet</strong>
            </p>
          </div>
        </div>

        <div className="flex items-center flex-wrap gap-2">
          {/* Chart Type Switcher */}
          <div className="flex items-center bg-zinc-950 border border-zinc-800 rounded-lg p-0.5 text-xs">
            <button
              onClick={() => setChartType('PIE')}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md font-medium transition-all cursor-pointer ${
                chartType === 'PIE'
                  ? 'bg-zinc-800 text-cyan-300 font-semibold shadow-sm'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              <PieChartIcon className="w-3.5 h-3.5" />
              <span>กราฟวงกลม (Donut)</span>
            </button>
            <button
              onClick={() => setChartType('BAR')}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-md font-medium transition-all cursor-pointer ${
                chartType === 'BAR'
                  ? 'bg-zinc-800 text-cyan-300 font-semibold shadow-sm'
                  : 'text-zinc-400 hover:text-zinc-200'
              }`}
            >
              <BarChart3 className="w-3.5 h-3.5" />
              <span>กราฟแท่ง (Stacked)</span>
            </button>
          </div>

          {/* Sync Button */}
          <button
            onClick={handleSyncBinance}
            disabled={isSyncing}
            className="flex items-center space-x-1.5 px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 active:bg-emerald-700 text-white shadow-sm shadow-emerald-600/30 transition-all disabled:opacity-50 cursor-pointer"
            title="ซิงค์ยอดเงินล่าสุดจาก Binance API"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${isSyncing ? 'animate-spin' : ''}`} />
            <span>{isSyncing ? 'กำลังซิงค์...' : 'ซิงค์จาก Binance'}</span>
          </button>

          {/* If rendered in modal, show full screen or close button */}
          {isModal && onCloseModal && (
            <button
              onClick={onCloseModal}
              className="p-1.5 rounded-lg bg-zinc-800 hover:bg-zinc-700 text-zinc-400 hover:text-zinc-100 border border-zinc-700 cursor-pointer"
              title="ปิดหน้าต่าง Pop-up"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {/* Sync feedback notification */}
      {syncFeedback && (
        <div
          className={`px-4 py-2.5 rounded-lg text-xs flex items-center justify-between transition-all ${
            syncFeedback.type === 'success'
              ? 'bg-emerald-950/60 border border-emerald-800/60 text-emerald-300'
              : syncFeedback.type === 'warn'
              ? 'bg-amber-950/60 border border-amber-800/60 text-amber-300'
              : 'bg-rose-950/60 border border-rose-800/60 text-rose-300'
          }`}
        >
          <div className="flex items-center space-x-2">
            {syncFeedback.type === 'success' ? (
              <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0" />
            ) : (
              <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
            )}
            <span>{syncFeedback.msg}</span>
          </div>
          <button
            onClick={() => setSyncFeedback(null)}
            className="text-zinc-400 hover:text-zinc-200 text-xs font-bold cursor-pointer"
          >
            ✕
          </button>
        </div>
      )}

      {/* 2. Main Visualization Section (Charts + Category Cards) */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        {/* Left Column: Recharts Visualization (7 Cols) */}
        <div className="lg:col-span-7 bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 flex flex-col justify-between">
          <div className="flex items-center justify-between pb-3 border-b border-zinc-800">
            <div>
              <h2 className="text-sm font-bold text-zinc-100 flex items-center space-x-2">
                <span>แผนภาพสัดส่วนสินทรัพย์ (Asset Distribution)</span>
              </h2>
              <span className="text-[11px] text-zinc-400">
                {chartType === 'PIE'
                  ? 'คลิกส่วนของกระเป๋าเพื่อกรองรายการสินทรัพย์ด้านล่าง'
                  : 'กระจายตัวของแต่ละเหรียญในแต่ละประเภทกระเป๋า'}
              </span>
            </div>

            <div className="text-right">
              <span className="text-[10px] text-zinc-400">มูลค่าพอร์ตรวม (Total Equity)</span>
              <div className="text-lg font-bold font-mono text-emerald-400">
                {formatSnapshotCurrency(equity)}
              </div>
            </div>
          </div>

          {/* Chart Canvas */}
          <div className="h-72 w-full mt-4 flex items-center justify-center relative">
            {chartType === 'PIE' ? (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Tooltip content={<CustomPieTooltip />} />
                  <Pie
                    data={pieChartData}
                    cx="50%"
                    cy="50%"
                    innerRadius={70}
                    outerRadius={105}
                    paddingAngle={3}
                    dataKey="value"
                    onClick={(entry: any) => {
                      const cat = entry?.category ?? entry?.payload?.category;
                      if (cat) {
                        setSelectedCategory((prev) => (prev === cat ? null : cat));
                      }
                    }}
                    cursor="pointer"
                  >
                    {pieChartData.map((entry, index) => (
                      <Cell
                        key={`cell-${index}`}
                        fill={entry.color}
                        stroke={selectedCategory === entry.category ? '#ffffff' : '#18181b'}
                        strokeWidth={selectedCategory === entry.category ? 3 : 1.5}
                        className="transition-all hover:opacity-90 cursor-pointer"
                      />
                    ))}
                  </Pie>
                  <Legend
                    verticalAlign="bottom"
                    height={36}
                    formatter={(value, entry: any) => {
                      const item = pieChartData.find((p) => p.name === value);
                      return (
                        <span className="text-xs text-zinc-300 font-medium">
                          {value} ({item?.pct}%)
                        </span>
                      );
                    }}
                  />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barChartData} margin={{ top: 20, right: 20, left: 0, bottom: 20 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#27272a" />
                  <XAxis dataKey="asset" stroke="#71717a" fontSize={12} />
                  <YAxis stroke="#71717a" fontSize={12} tickFormatter={(val) => `$${val}`} />
                  <Tooltip content={<CustomBarTooltip />} />
                  <Legend verticalAlign="top" height={36} />
                  <Bar
                    dataKey="TRADING_BOT"
                    name="Trading Bot"
                    stackId="a"
                    fill={CATEGORY_CONFIG.TRADING_BOT.color}
                    radius={[0, 0, 0, 0]}
                  />
                  <Bar
                    dataKey="PORTFOLIO_MARGIN"
                    name="Portfolio Margin"
                    stackId="a"
                    fill={CATEGORY_CONFIG.PORTFOLIO_MARGIN.color}
                    radius={[0, 0, 0, 0]}
                  />
                  <Bar
                    dataKey="EARN"
                    name="Simple Earn"
                    stackId="a"
                    fill={CATEGORY_CONFIG.EARN.color}
                    radius={[0, 0, 0, 0]}
                  />
                  <Bar
                    dataKey="SPOT"
                    name="Spot Wallet"
                    stackId="a"
                    fill={CATEGORY_CONFIG.SPOT.color}
                    radius={[4, 4, 0, 0]}
                  />
                </BarChart>
              </ResponsiveContainer>
            )}

            {/* Inner Center Label for Donut */}
            {chartType === 'PIE' && (
              <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none -mt-4">
                <span className="text-[10px] text-zinc-400 uppercase tracking-wider font-semibold">
                  {selectedCategory ? CATEGORY_CONFIG[selectedCategory]?.name : 'TOTAL ASSETS'}
                </span>
                <span className="text-base font-bold font-mono text-zinc-100">
                  {selectedCategory
                    ? formatSnapshotCurrency(subWallets.find((w) => w.category === selectedCategory)?.usdVal ?? 0)
                    : formatSnapshotCurrency(equity, 0)}
                </span>
                <span className={`text-[9px] font-mono ${hasVerifiedSnapshot ? 'text-emerald-400' : 'text-amber-400'}`}>
                  {hasVerifiedSnapshot ? 'VERIFIED SNAPSHOT' : 'ILLUSTRATIVE ONLY'}
                </span>
              </div>
            )}
          </div>

          <div className="pt-3 border-t border-zinc-800/80 flex items-center justify-between text-xs text-zinc-400">
            <span>
              {selectedCategory ? (
                <button
                  onClick={() => setSelectedCategory(null)}
                  className="text-cyan-400 hover:underline flex items-center space-x-1 cursor-pointer"
                >
                  <span>ล้างตัวกรอง ({CATEGORY_CONFIG[selectedCategory]?.name})</span>
                  <span>✕</span>
                </button>
              ) : (
                <span>แสดงทุกกระเป๋า (All Sub-Wallets)</span>
              )}
            </span>
            {lastSyncTime && (
              <span className="text-[10px] font-mono text-zinc-500">
                อัปเดตล่าสุด: {new Date(lastSyncTime).toLocaleTimeString()}
              </span>
            )}
          </div>
        </div>

        {/* Right Column: Interactive Sub-Wallet Cards & Relevant Deep Links (5 Cols) */}
        <div className="lg:col-span-5 space-y-3 flex flex-col justify-between">
          <div className="space-y-2.5">
            <div className="text-xs font-bold text-zinc-300 uppercase tracking-wider flex items-center justify-between">
              <span>กระเป๋าย่อยและสัดส่วน (Sub-Wallets)</span>
              <span className="text-[10px] text-zinc-500 normal-case">กดการ์ดเพื่อเลือกดูเฉพาะหมวด</span>
            </div>

            {subWallets.length === 0 && (
              <div className="p-3 rounded-xl border border-amber-900/60 bg-amber-950/20 text-xs text-amber-300">
                No verified Testnet account snapshot is available. Connect the Python worker and complete
                a signed Testnet account sync before treating balances as evidence.
              </div>
            )}
            {subWallets.map((wallet) => {
              const cfg = CATEGORY_CONFIG[wallet.category] || CATEGORY_CONFIG.SPOT;
              const isSelected = selectedCategory === wallet.category;

              return (
                <div
                  key={wallet.walletName}
                  onClick={() =>
                    setSelectedCategory((prev) => (prev === wallet.category ? null : wallet.category))
                  }
                  className={`p-3 rounded-xl border transition-all cursor-pointer ${
                    isSelected
                      ? 'bg-zinc-800/90 border-cyan-500 shadow-md ring-1 ring-cyan-500/50'
                      : `${cfg.bgColor} ${cfg.borderColor} hover:border-zinc-600`
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <div className="flex items-center space-x-2.5">
                      <div className="p-1.5 rounded-lg bg-zinc-900 border border-zinc-800">
                        {cfg.icon}
                      </div>
                      <div>
                        <div className="flex items-center space-x-2">
                          <span className="text-xs font-bold text-zinc-100">{wallet.walletName}</span>
                          <span
                            className="text-[10px] px-1.5 py-0.2 rounded font-mono font-semibold"
                            style={{ color: cfg.color, backgroundColor: `${cfg.color}15` }}
                          >
                            {wallet.pctOfTotal}%
                          </span>
                        </div>
                        <p className="text-[10px] text-zinc-400 line-clamp-1 mt-0.5">
                          {cfg.description}
                        </p>
                      </div>
                    </div>

                    <div className="text-right">
                      <div className="text-xs font-bold font-mono text-zinc-100">
                        {formatSnapshotCurrency(wallet.usdVal)}
                      </div>
                      <div className="text-[10px] font-mono text-zinc-400">
                        {hasVerifiedSnapshot ? `≈ ${wallet.btcVal.toFixed(6)} BTC` : 'UNKNOWN'}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Relevant Action Links Box ("ลิ้งค์ไปที่เกี่ยวข้องอีกที") */}
          <div className="bg-zinc-900/90 border border-zinc-800 rounded-xl p-3.5 space-y-2.5">
            <div className="flex items-center justify-between border-b border-zinc-800 pb-2">
              <span className="text-xs font-bold text-zinc-200 flex items-center space-x-1.5">
                <ArrowRight className="w-3.5 h-3.5 text-cyan-400" />
                <span>ลิ้งค์และระบบที่เกี่ยวข้อง (Quick Actions)</span>
              </span>
              <span className="text-[10px] text-zinc-400">Blessing AI & Binance</span>
            </div>

            <div className="grid grid-cols-2 gap-2 text-xs">
              {/* Cockpit / Grid Baskets Link */}
              <button
                type="button"
                onClick={() => onNavigateTab && onNavigateTab('cockpit')}
                className="p-2 rounded-lg bg-zinc-950/70 border border-zinc-800 hover:border-cyan-700 text-left transition-colors cursor-pointer group"
              >
                <div className="flex items-center justify-between text-cyan-400 mb-0.5">
                  <span className="font-semibold text-[11px] group-hover:underline">Cockpit & Grids</span>
                  <Bot className="w-3 h-3" />
                </div>
                <p className="text-[10px] text-zinc-400">ดูคำสั่ง Trading Bot & Baskets</p>
              </button>

              {/* Risk Governor / Leverage Link */}
              <button
                type="button"
                onClick={() => onNavigateTab && onNavigateTab('cockpit')}
                className="p-2 rounded-lg bg-zinc-950/70 border border-zinc-800 hover:border-indigo-700 text-left transition-colors cursor-pointer group"
              >
                <div className="flex items-center justify-between text-indigo-400 mb-0.5">
                  <span className="font-semibold text-[11px] group-hover:underline">Risk Governor</span>
                  <Scale className="w-3 h-3" />
                </div>
                <p className="text-[10px] text-zinc-400">ตรวจสอบ Portfolio Margin</p>
              </button>

              {/* BigQuery Analytics Link */}
              <button
                type="button"
                onClick={() => onNavigateTab && onNavigateTab('bigquery')}
                className="p-2 rounded-lg bg-zinc-950/70 border border-zinc-800 hover:border-emerald-700 text-left transition-colors cursor-pointer group"
              >
                <div className="flex items-center justify-between text-emerald-400 mb-0.5">
                  <span className="font-semibold text-[11px] group-hover:underline">BigQuery Ledger</span>
                  <Database className="w-3 h-3" />
                </div>
                <p className="text-[10px] text-zinc-400">สถิติ & ประวัติการจัดสรรเงิน</p>
              </button>

              {/* Binance API Key Settings Link */}
              <button
                type="button"
                onClick={() => onOpenKeyModal && onOpenKeyModal()}
                className="p-2 rounded-lg bg-zinc-950/70 border border-zinc-800 hover:border-amber-700 text-left transition-colors cursor-pointer group"
              >
                <div className="flex items-center justify-between text-amber-400 mb-0.5">
                  <span className="font-semibold text-[11px] group-hover:underline">Binance API Keys</span>
                  <Lock className="w-3 h-3" />
                </div>
                <p className="text-[10px] text-zinc-400">สลับโปรไฟล์ & เช็กสิทธิ์ API</p>
              </button>
            </div>

            {/* Direct External Links to Binance Portals */}
            <div className="pt-2 border-t border-zinc-800/80 flex items-center justify-between text-[11px]">
              <span className="text-zinc-500">พอร์ทัล Binance:</span>
              <div className="flex items-center space-x-3">
                <a
                  href="https://www.binance.com/en/trading-bots"
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-cyan-400 hover:underline flex items-center space-x-1"
                >
                  <span>Bots</span>
                  <ExternalLink className="w-2.5 h-2.5" />
                </a>
                <a
                  href="https://www.binance.com/en/my/wallet/account/portfolio-margin"
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-indigo-400 hover:underline flex items-center space-x-1"
                >
                  <span>PM Margin</span>
                  <ExternalLink className="w-2.5 h-2.5" />
                </a>
                <a
                  href="https://www.binance.com/en/earn"
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-amber-400 hover:underline flex items-center space-x-1"
                >
                  <span>Earn</span>
                  <ExternalLink className="w-2.5 h-2.5" />
                </a>
                <a
                  href="https://www.binance.com/en/my/wallet/account/main"
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-emerald-400 hover:underline flex items-center space-x-1"
                >
                  <span>Spot</span>
                  <ExternalLink className="w-2.5 h-2.5" />
                </a>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 3. Detailed Two-Layer Asset Table with Location Breakdown */}
      <div className="bg-zinc-900/80 border border-zinc-800 rounded-xl p-5 space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3 pb-3 border-b border-zinc-800">
          <div>
            <h3 className="text-sm font-bold text-zinc-100 flex items-center space-x-2">
              <Layers className="w-4 h-4 text-cyan-400" />
              <span>
                รายละเอียดเหรียญและการจัดสรร (Layer 1: Asset ➔ Layer 2: Location Breakdown)
              </span>
            </h3>
            <p className="text-[11px] text-zinc-400 mt-0.5">
              {selectedCategory
                ? `กำลังกรองเฉพาะเหรียญที่ถืออยู่ใน "${CATEGORY_CONFIG[selectedCategory]?.name}"`
                : 'แสดงสินทรัพย์ทั้งหมดพร้อมสัดส่วนที่กระจายอยู่ในแต่ละกระเป๋า'}
            </p>
          </div>

          <div className="flex items-center space-x-2">
            <div className="relative">
              <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-zinc-500" />
              <input
                type="text"
                value={searchFilter}
                onChange={(e) => setSearchFilter(e.target.value)}
                placeholder="ค้นหาเหรียญ (USDC, BNB...)"
                className="w-44 pl-8 pr-2.5 py-1 text-xs bg-zinc-950 border border-zinc-800 rounded-lg text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-cyan-600"
              />
            </div>

            {selectedCategory && (
              <button
                onClick={() => setSelectedCategory(null)}
                className="px-2.5 py-1 rounded-lg text-xs bg-zinc-800 hover:bg-zinc-700 text-zinc-300 border border-zinc-700 cursor-pointer"
              >
                แสดงทั้งหมด
              </button>
            )}
          </div>
        </div>

        {/* Asset Cards Grid */}
        <div className="space-y-3">
          {filteredAssets.length === 0 ? (
            <div className="p-8 text-center text-zinc-500 text-xs bg-zinc-950/40 rounded-xl border border-dashed border-zinc-800">
              ไม่พบเหรียญตามเงื่อนไขการค้นหาหรือตัวกรองที่เลือก
            </div>
          ) : (
            filteredAssets.map((assetItem) => (
              <div
                key={assetItem.asset}
                className="bg-zinc-950/60 border border-zinc-800 rounded-xl p-4 space-y-3 hover:border-zinc-700 transition-colors"
              >
                {/* Header Row */}
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center space-x-3">
                    <div className="w-9 h-9 rounded-lg bg-zinc-800/90 border border-zinc-700 flex items-center justify-center font-mono font-bold text-xs text-zinc-100 shadow-inner">
                      {assetItem.asset}
                    </div>
                    <div>
                      <div className="flex items-center space-x-2">
                        <span className="text-sm font-bold text-zinc-100">{assetItem.asset}</span>
                        <span className="text-xs text-zinc-400 font-mono">
                              {formatSnapshotNumber(assetItem.totalQty, assetItem.totalQty > 1000 ? 2 : 4)}{' '}
                              {hasVerifiedSnapshot ? assetItem.asset : ''}
                        </span>
                      </div>
                      <div className="text-[11px] text-zinc-400 mt-0.5">
                        ราคา: <strong className="text-zinc-200 font-mono">{formatSnapshotCurrency(assetItem.unitPrice)}</strong> • สัดส่วนพอร์ต: <strong className="text-cyan-400 font-mono">{hasVerifiedSnapshot ? `${assetItem.pctOfPortfolio}%` : 'UNKNOWN'}</strong>
                      </div>
                    </div>
                  </div>

                  <div className="text-right">
                    <div className="text-sm font-bold font-mono text-emerald-400">
                      {formatSnapshotCurrency(assetItem.totalUsdVal)}
                    </div>
                    <div className="text-[10px] text-zinc-400">มูลค่ารวม USD</div>
                  </div>
                </div>

                {/* Layer 2: Breakdown of Locations for this Coin */}
                <div className="pt-2 border-t border-zinc-800/80">
                  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2.5">
                    {assetItem.allocations.map((al, idx) => {
                      const cfg = CATEGORY_CONFIG[al.category] || CATEGORY_CONFIG.SPOT;
                      const isHighlighted = selectedCategory === al.category;

                      return (
                        <div
                          key={idx}
                          className={`p-2.5 rounded-lg border flex flex-col justify-between transition-all ${
                            isHighlighted
                              ? 'bg-zinc-800/90 border-cyan-500 shadow-sm'
                              : `${cfg.bgColor} ${cfg.borderColor}`
                          }`}
                        >
                          <div>
                            <div className="flex items-center justify-between mb-1">
                              <div className="flex items-center space-x-1.5">
                                {cfg.icon}
                                <span className="text-xs font-semibold text-zinc-200">{al.location}</span>
                              </div>
                              <span
                                className="px-1.5 py-0.2 rounded text-[10px] font-mono font-bold"
                                style={{ color: cfg.color }}
                              >
                                {al.pctOfAsset}%
                              </span>
                            </div>
                            <div className="text-xs font-mono font-semibold text-zinc-100">
                              {formatSnapshotNumber(al.qty, al.qty > 1000 ? 2 : al.qty < 1 ? 6 : 2)}{' '}
                              <span className="text-[10px] font-normal text-zinc-400">{hasVerifiedSnapshot ? assetItem.asset : ''}</span>
                            </div>
                          </div>

                          <div className="mt-2 pt-1.5 border-t border-zinc-800/70 flex items-center justify-between text-[10px]">
                            <span className="text-zinc-400">{al.detail || 'Allocation'}</span>
                            <span className="text-emerald-400 font-mono font-medium">
                              {formatSnapshotCurrency(al.usdVal, al.usdVal >= 1 ? 2 : 4)}
                            </span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
