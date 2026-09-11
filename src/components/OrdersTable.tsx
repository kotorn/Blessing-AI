import React, { useState, useMemo } from 'react';
import {
  Search,
  Filter,
  ArrowUpDown,
  ExternalLink,
  CheckCircle2,
  Clock,
  Layers,
  ArrowRight,
  TrendingUp,
  ShieldAlert,
} from 'lucide-react';

import { ExecutionOrder, OrderStatus, OrderVenue } from '../types/orders';

interface OrdersTableProps {
  orders: ExecutionOrder[];
  selectedOrderId: string | null;
  onSelectOrder: (order: ExecutionOrder) => void;
}

export const OrdersTable: React.FC<OrdersTableProps> = ({
  orders = [],
  selectedOrderId,
  onSelectOrder,
}) => {
  const [venueFilter, setVenueFilter] = useState<'ALL' | OrderVenue>('ALL');
  const [statusFilter, setStatusFilter] = useState<'ALL' | OrderStatus>('ALL');
  const [symbolFilter, setSymbolFilter] = useState<string>('ALL');
  const [strategyFilter, setStrategyFilter] = useState<string>('ALL');
  const [searchQuery, setSearchQuery] = useState<string>('');

  

  // Apply filters
  const filteredOrders = useMemo(() => {
    return orders.filter((o) => {
      if (venueFilter !== 'ALL' && o.venue !== venueFilter) return false;
      if (statusFilter !== 'ALL' && o.status !== statusFilter) return false;
      if (symbolFilter !== 'ALL' && o.symbol !== symbolFilter) return false;
      if (strategyFilter !== 'ALL' && o.strategy !== strategyFilter) return false;
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        return (
          o.clientOrderId.toLowerCase().includes(q) ||
          o.basketId.toLowerCase().includes(q) ||
          o.symbol.toLowerCase().includes(q)
        );
      }
      return true;
    });
  }, [orders, venueFilter, statusFilter, symbolFilter, strategyFilter, searchQuery]);

  return (
    <div className="bg-zinc-900/90 border border-zinc-800 rounded-2xl overflow-hidden shadow-xl shadow-black/20 space-y-4 p-4 sm:p-5">
      {/* Table Controls & Filter Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        {/* Left: Search */}
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500" />
          <input
            type="text"
            placeholder="Search Order ID, Basket, Symbol..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full pl-8 pr-3 py-1.5 bg-zinc-950 border border-zinc-800 rounded-lg text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-cyan-500/70"
          />
        </div>

        {/* Right: Filters */}
        <div className="flex flex-wrap items-center gap-2 text-xs">
          {/* Venue */}
          <select
            value={venueFilter}
            onChange={(e) => setVenueFilter(e.target.value as any)}
            className="px-2.5 py-1.5 bg-zinc-950 border border-zinc-800 rounded-lg text-zinc-300 focus:outline-none cursor-pointer"
          >
            <option value="ALL">All Venues</option>
            <option value="binance_usdm">Binance USDⓈ-M Futures</option>
            <option value="binance_spot">Binance Spot</option>
          </select>

          {/* Status */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as any)}
            className="px-2.5 py-1.5 bg-zinc-950 border border-zinc-800 rounded-lg text-zinc-300 focus:outline-none cursor-pointer"
          >
            <option value="ALL">All Statuses</option>
            <option value="FILLED">Filled</option>
            <option value="NEW">Resting (Pending)</option>
          </select>

          {/* Symbol */}
          <select
            value={symbolFilter}
            onChange={(e) => setSymbolFilter(e.target.value)}
            className="px-2.5 py-1.5 bg-zinc-950 border border-zinc-800 rounded-lg text-zinc-300 focus:outline-none cursor-pointer"
          >
            <option value="ALL">All Symbols</option>
            <option value="BTCUSDT">BTCUSDT</option>
            <option value="ETHUSDT">ETHUSDT</option>
          </select>

          {/* Strategy */}
          <select
            value={strategyFilter}
            onChange={(e) => setStrategyFilter(e.target.value)}
            className="px-2.5 py-1.5 bg-zinc-950 border border-zinc-800 rounded-lg text-zinc-300 focus:outline-none cursor-pointer"
          >
            <option value="ALL">All Alpha Engines</option>
            <option value="Structural Grid">Structural Grid</option>
            <option value="Exposure Recovery">Exposure Recovery</option>
          </select>
        </div>
      </div>

      {/* Orders Table */}
      <div className="overflow-x-auto border border-zinc-800/80 rounded-xl">
        <table className="w-full text-left text-xs">
          <thead className="bg-zinc-950/80 border-b border-zinc-800 text-[10px] uppercase font-mono text-zinc-400">
            <tr>
              <th className="py-2.5 px-3">Order / Client ID</th>
              <th className="py-2.5 px-3">Symbol / Venue</th>
              <th className="py-2.5 px-3">Strategy Attribution</th>
              <th className="py-2.5 px-3">Side / Type</th>
              <th className="py-2.5 px-3 text-right">Price ($)</th>
              <th className="py-2.5 px-3 text-right">Size</th>
              <th className="py-2.5 px-3 text-right">Notional ($)</th>
              <th className="py-2.5 px-3 text-center">Lifecycle Status</th>
              <th className="py-2.5 px-3 text-center">Source</th>
              <th className="py-2.5 px-3 text-center">Source</th>
              <th className="py-2.5 px-3 text-center">Action</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/60 font-mono">
            {filteredOrders.length === 0 ? (
              <tr>
                <td colSpan={9} className="py-8 text-center text-zinc-500 font-sans">
                  No orders match current filter criteria.
                </td>
              </tr>
            ) : (
              filteredOrders.map((ord) => {
                const isSelected = ord.id === selectedOrderId;
                const isBuy = ord.side === 'BUY';
                const isFilled = ord.status === 'FILLED';

                return (
                  <tr
                    key={ord.id}
                    onClick={() => onSelectOrder(ord)}
                    className={`transition-colors cursor-pointer ${
                      isSelected
                        ? 'bg-cyan-950/40 hover:bg-cyan-950/50'
                        : 'hover:bg-zinc-800/40'
                    }`}
                  >
                    {/* Order ID */}
                    <td className="py-3 px-3">
                      <div className="font-bold text-zinc-100 flex items-center space-x-1.5">
                        <span>{ord.clientOrderId}</span>
                      </div>
                      <div className="text-[10px] text-zinc-500 font-sans">
                        Basket: {ord.basketId}
                      </div>
                    </td>

                    {/* Symbol / Venue */}
                    <td className="py-3 px-3">
                      <div className="font-bold text-zinc-200">{ord.symbol}</div>
                      <div className="text-[10px] text-cyan-400 font-sans uppercase">
                        {ord.venue === 'binance_usdm' ? 'USDⓈ-M Futures' : 'Spot'}
                      </div>
                    </td>

                    {/* Strategy Attribution */}
                    <td className="py-3 px-3 font-sans">
                      <span
                        className={`px-2 py-0.5 rounded text-[10px] font-semibold border ${
                          ord.strategy === 'Structural Grid'
                            ? 'bg-indigo-950 text-indigo-300 border-indigo-800/80'
                            : 'bg-orange-950 text-orange-300 border-orange-800/80'
                        }`}
                      >
                        {ord.strategy}
                      </span>
                    </td>

                    {/* Side / Type */}
                    <td className="py-3 px-3">
                      <div className="flex items-center space-x-1">
                        <span
                          className={`font-bold ${
                            isBuy ? 'text-emerald-400' : 'text-rose-400'
                          }`}
                        >
                          {ord.side}
                        </span>
                        <span className="text-[10px] text-zinc-400 font-sans">
                          {ord.type === 'LIMIT_MAKER' ? 'MAKER' : ord.type}
                        </span>
                      </div>
                    </td>

                    {/* Price */}
                    <td className="py-3 px-3 text-right text-zinc-200 font-semibold">
                      ${ord.price.toLocaleString(undefined, { minimumFractionDigits: 1 })}
                    </td>

                    {/* Size */}
                    <td className="py-3 px-3 text-right text-zinc-300">
                      {ord.size}
                    </td>

                    {/* Notional */}
                    <td className="py-3 px-3 text-right text-zinc-400">
                      ${Math.round(ord.valueUsd).toLocaleString()}
                    </td>

                    {/* Status */}
                    <td className="py-3 px-3 text-center">
                      <span
                        className={`inline-flex items-center space-x-1 px-2 py-0.5 rounded text-[10px] font-bold border ${
                          isFilled
                            ? 'bg-emerald-950 text-emerald-300 border-emerald-800/80'
                            : 'bg-amber-950 text-amber-300 border-amber-800/80'
                        }`}
                      >
                        {isFilled ? (
                          <CheckCircle2 className="w-3 h-3" />
                        ) : (
                          <Clock className="w-3 h-3" />
                        )}
                        <span>{isFilled ? 'FILLED' : 'RESTING'}</span>
                      </span>
                    </td>

                    {/* Action */}
                    <td className="py-3 px-3 text-center">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectOrder(ord);
                        }}
                        className={`px-2 py-1 rounded text-[10px] font-sans font-semibold transition-colors cursor-pointer ${
                          isSelected
                            ? 'bg-cyan-600 text-white'
                            : 'bg-zinc-800 hover:bg-zinc-700 text-zinc-300'
                        }`}
                      >
                        Trace
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};
