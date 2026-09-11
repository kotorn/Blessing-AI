import React, { useEffect, useState, useRef } from 'react';
import { Activity, Zap, TrendingUp, TrendingDown, ArrowRightLeft } from 'lucide-react';
import { InstrumentData } from '../types';

interface MicrostructureMonitorProps {
  symbol: string;
}

interface OrderBookEntry {
  price: number;
  qty: number;
}

export const MicrostructureMonitor: React.FC<MicrostructureMonitorProps> = ({ symbol }) => {
  const [bids, setBids] = useState<OrderBookEntry[]>([]);
  const [asks, setAsks] = useState<OrderBookEntry[]>([]);
  const [cvd, setCvd] = useState<number>(0);
  const [lastPrice, setLastPrice] = useState<number>(0);
  const [imbalance, setImbalance] = useState<number>(50); // 0-100, 50 is balanced
  
  const wsDepthRef = useRef<WebSocket | null>(null);
  const wsTradesRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    // Reset state on symbol change
    setBids([]);
    setAsks([]);
    setCvd(0);
    setImbalance(50);
    
    const lowerSymbol = symbol.toLowerCase();
    
    // 1. OrderBook WebSocket (10 levels, 100ms updates)
    const depthUrl = `wss://stream.binance.com:9443/ws/${lowerSymbol}@depth10@100ms`;
    wsDepthRef.current = new WebSocket(depthUrl);
    
    wsDepthRef.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.bids && data.asks) {
          const parsedBids = data.bids.slice(0, 5).map((b: string[]) => ({ price: parseFloat(b[0]), qty: parseFloat(b[1]) }));
          const parsedAsks = data.asks.slice(0, 5).map((a: string[]) => ({ price: parseFloat(a[0]), qty: parseFloat(a[1]) }));
          
          setBids(parsedBids);
          setAsks(parsedAsks);
          
          // Calculate volume imbalance for top 5 levels
          const bidVol = parsedBids.reduce((sum: number, b: OrderBookEntry) => sum + b.qty, 0);
          const askVol = parsedAsks.reduce((sum: number, a: OrderBookEntry) => sum + a.qty, 0);
          const totalVol = bidVol + askVol;
          if (totalVol > 0) {
            setImbalance((bidVol / totalVol) * 100);
          }
        }
      } catch (err) {}
    };

    // 2. Aggregate Trades WebSocket (for CVD and Last Price)
    const tradesUrl = `wss://stream.binance.com:9443/ws/${lowerSymbol}@aggTrade`;
    wsTradesRef.current = new WebSocket(tradesUrl);
    
    wsTradesRef.current.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.p && data.q) {
          const price = parseFloat(data.p);
          const qty = parseFloat(data.q);
          const isBuyerMaker = data.m; // True if the buyer is the maker (sell order hit the bid)
          
          setLastPrice(price);
          
          // If buyer is maker, it was a market sell (negative delta)
          // If buyer is NOT maker, it was a market buy (positive delta)
          const delta = isBuyerMaker ? -qty : qty;
          
          setCvd((prev) => {
            // Decay CVD slowly to keep it readable, or just let it accumulate
            // For UI purposes, we'll bound it or decay it slightly so it doesn't go to infinity
            const newCvd = prev + delta;
            // Apply slight decay (0.1% per trade) to keep it centered around recent activity
            return newCvd * 0.999;
          });
        }
      } catch (err) {}
    };

    return () => {
      if (wsDepthRef.current) wsDepthRef.current.close();
      if (wsTradesRef.current) wsTradesRef.current.close();
    };
  }, [symbol]);

  // Calculations for UI
  const spread = asks.length > 0 && bids.length > 0 ? asks[0].price - bids[0].price : 0;
  const spreadBps = asks.length > 0 ? (spread / asks[0].price) * 10000 : 0;
  
  const isCvdBullish = cvd > 0;
  
  return (
    <div className="bg-zinc-950 border border-zinc-800/80 rounded-xl overflow-hidden flex flex-col shadow-sm">
      <div className="px-4 py-3 border-b border-zinc-800/80 flex items-center justify-between bg-zinc-900/40">
        <div className="flex items-center space-x-2.5">
          <div className="w-8 h-8 rounded-lg bg-indigo-950 border border-indigo-800/50 flex items-center justify-center text-indigo-400 shadow-sm shadow-indigo-900/20">
            <Activity className="w-4 h-4" />
          </div>
          <div>
            <h3 className="text-sm font-bold text-zinc-100 flex items-center space-x-1.5">
              <span>L2 Microstructure & Order Flow</span>
              <span className="px-1.5 py-0.5 rounded text-[9px] font-bold bg-emerald-950/50 text-emerald-400 border border-emerald-800/50 flex items-center shadow-sm">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 mr-1 animate-pulse" />
                LIVE
              </span>
            </h3>
            <p className="text-[11px] text-zinc-400 font-mono">
              Real-time WebSocket • Imbalance • CVD • Spread
            </p>
          </div>
        </div>
        
        <div className="text-right">
          <div className="text-lg font-mono font-bold text-zinc-100">
            {lastPrice > 0 ? lastPrice.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '---'}
          </div>
          <div className="text-[10px] text-zinc-400 font-mono flex items-center justify-end space-x-1">
            <ArrowRightLeft className="w-3 h-3" />
            <span>Spread: {spreadBps.toFixed(2)} bps</span>
          </div>
        </div>
      </div>

      <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-6">
        
        {/* Left Col: Order Book */}
        <div className="space-y-3">
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-500 uppercase tracking-wider">
            <span>Ask (Sell)</span>
            <span>Qty</span>
          </div>
          <div className="space-y-0.5 font-mono text-[11px]">
            {asks.slice().reverse().map((ask, i) => (
              <div key={`ask-${i}`} className="flex justify-between items-center relative py-0.5 px-1 rounded-sm overflow-hidden group hover:bg-rose-950/20">
                <div 
                  className="absolute right-0 top-0 bottom-0 bg-rose-950/30 -z-10" 
                  style={{ width: `${Math.min(100, (ask.qty / 5) * 100)}%` }}
                />
                <span className="text-rose-400">{ask.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className="text-zinc-300">{ask.qty.toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</span>
              </div>
            ))}
          </div>
          
          <div className="py-1 flex items-center justify-center">
            <span className="text-[10px] font-mono font-bold text-zinc-500 bg-zinc-900 px-2 py-0.5 rounded border border-zinc-800">
              SPREAD {(spread).toFixed(2)}
            </span>
          </div>
          
          <div className="space-y-0.5 font-mono text-[11px]">
            {bids.map((bid, i) => (
              <div key={`bid-${i}`} className="flex justify-between items-center relative py-0.5 px-1 rounded-sm overflow-hidden group hover:bg-emerald-950/20">
                <div 
                  className="absolute right-0 top-0 bottom-0 bg-emerald-950/30 -z-10" 
                  style={{ width: `${Math.min(100, (bid.qty / 5) * 100)}%` }}
                />
                <span className="text-emerald-400">{bid.price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                <span className="text-zinc-300">{bid.qty.toLocaleString(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</span>
              </div>
            ))}
          </div>
          <div className="flex items-center justify-between text-[10px] font-bold text-zinc-500 uppercase tracking-wider mt-1">
            <span>Bid (Buy)</span>
            <span>Qty</span>
          </div>
        </div>

        {/* Right Col: Imbalance & CVD */}
        <div className="space-y-6 flex flex-col justify-center">
          
          {/* Order Book Imbalance */}
          <div className="bg-zinc-900/50 p-4 rounded-xl border border-zinc-800/80 space-y-3">
            <div className="flex justify-between items-center text-xs">
              <span className="text-zinc-400 font-medium">L2 Book Imbalance</span>
              <span className="font-mono font-bold text-zinc-200">
                {imbalance.toFixed(1)}% / {(100 - imbalance).toFixed(1)}%
              </span>
            </div>
            
            {/* Imbalance Bar */}
            <div className="h-2 w-full bg-zinc-800 rounded-full overflow-hidden flex">
              <div 
                className="h-full bg-emerald-500 transition-all duration-300"
                style={{ width: `${imbalance}%` }}
              />
              <div 
                className="h-full bg-rose-500 transition-all duration-300"
                style={{ width: `${100 - imbalance}%` }}
              />
            </div>
            
            <div className="flex justify-between items-center text-[10px] font-mono font-semibold">
              <span className="text-emerald-400">BID VOL</span>
              <span className="text-rose-400">ASK VOL</span>
            </div>
          </div>
          
          {/* Cumulative Volume Delta (CVD) */}
          <div className="bg-zinc-900/50 p-4 rounded-xl border border-zinc-800/80 space-y-3">
            <div className="flex justify-between items-center text-xs">
              <span className="text-zinc-400 font-medium">Cumulative Vol Delta (CVD)</span>
              <div className={`flex items-center space-x-1 font-mono font-bold ${isCvdBullish ? 'text-emerald-400' : 'text-rose-400'}`}>
                {isCvdBullish ? <TrendingUp className="w-3.5 h-3.5" /> : <TrendingDown className="w-3.5 h-3.5" />}
                <span>{cvd > 0 ? '+' : ''}{cvd.toFixed(2)}</span>
              </div>
            </div>
            
            <p className="text-[10px] text-zinc-500 leading-relaxed">
              Real-time aggregation of aggressor order volume. Positive CVD indicates aggressive market buying lifting the ask. Negative CVD indicates aggressive market selling hitting the bid.
            </p>
          </div>
          
        </div>
        
      </div>
    </div>
  );
};
