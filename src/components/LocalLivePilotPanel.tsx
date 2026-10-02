import React, { useCallback, useEffect, useState } from 'react';
import { Activity, AlertTriangle, CheckCircle2, LoaderCircle, ShieldAlert } from 'lucide-react';
import { apiClient } from '../api/client';
import type { LocalLivePilotStrategyId } from '../backend/local-live-pilot';

type Campaign = {
  campaignId: string;
  status: string;
  strategyId: LocalLivePilotStrategyId;
  campaignExpiresAt?: string;
  version: number;
};

type PilotResponse = {
  campaign?: Campaign;
  campaignId?: string;
  strategyId?: LocalLivePilotStrategyId;
  campaignExpiresAt?: string;
  version?: number;
  status?: string;
  error?: string;
  evidence_status?: string;
  preflight?: { status?: string };
  worker?: { status?: string; mainnetLiveApproved?: boolean; orderSubmissionAttempts?: number };
  readiness?: { status?: string; canApprove?: boolean; canStart?: boolean; blockers?: string[] };
  preparation?: { status?: string; observedAt?: string | null };
  runtime?: {
    executionMode?: string; engineState?: string; mainnetLiveApproved?: boolean;
    orderSubmissionAttempts?: number | 'UNKNOWN';
  } | 'UNKNOWN';
  accounting?: {
    status?: string;
    netPnlUsdc?: string;
    peakNetPnlUsdc?: string;
    drawdownUsdc?: string;
    realizedPnlUsdc?: string;
    unrealizedPnlUsdc?: string;
    feesUsdc?: string;
    fundingUsdc?: string;
    slippageUsdc?: string;
    lastEventAt?: string | null;
    reason?: string | null;
  };
};

const STRATEGIES: Array<{ id: LocalLivePilotStrategyId; label: string }> = [
  { id: 'trend', label: 'Trend' },
  { id: 'shock', label: 'Shock' },
  { id: 'carry', label: 'Funding Carry' },
  { id: 'grid', label: 'Grid' },
];

function campaignFrom(value: PilotResponse): Campaign | null {
  if (value.campaign) return value.campaign;
  return typeof value.campaignId === 'string' && typeof value.status === 'string'
    ? {
        campaignId: value.campaignId,
        status: value.status,
        strategyId: value.strategyId || 'trend',
        campaignExpiresAt: value.campaignExpiresAt,
        version: value.version ?? 0,
      }
    : null;
}

export const LocalLivePilotPanel: React.FC = () => {
  const [strategyId, setStrategyId] = useState<LocalLivePilotStrategyId>('trend');
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('ยังไม่มีผลตรวจจากแคมเปญนี้');
  const [evidence, setEvidence] = useState<'UNKNOWN' | 'PASS' | 'FAIL'>('UNKNOWN');
  const [accounting, setAccounting] = useState<PilotResponse['accounting']>();
  const [readiness, setReadiness] = useState<PilotResponse['readiness']>();
  const [preparation, setPreparation] = useState<PilotResponse['preparation']>();
  const [runtime, setRuntime] = useState<PilotResponse['runtime']>();

  useEffect(() => {
    let active = true;
    void apiClient.get<PilotResponse>('/api/local/pilot/readiness')
      .then((res) => {
        if (!active) return;
        if (res.readiness) setReadiness(res.readiness);
      })
      .catch(() => {});
    return () => { active = false; };
  }, []);

  const runAction = useCallback(async (action: string, payload?: Record<string, unknown>) => {
    setBusy(true);
    setMessage('กำลังตรวจสอบกับระบบ');
    setEvidence('UNKNOWN');
    try {
      const result = await apiClient.post<PilotResponse>(`/api/local/pilot/${action}`, payload);
      const next = campaignFrom(result);
      if (next) setCampaign(next);
      let refreshFailed = false;
      if (next?.campaignId) {
        try {
          const latest = await apiClient.get<PilotResponse>(`/api/local/pilot/${encodeURIComponent(next.campaignId)}`);
          setAccounting(latest.accounting);
          setReadiness(latest.readiness);
          setPreparation(latest.preparation);
          setRuntime(latest.runtime);
        } catch {
          refreshFailed = true;
          setReadiness(undefined);
          setPreparation(undefined);
          setRuntime('UNKNOWN');
        }
      }
      setEvidence(result.evidence_status === 'VERIFIED' && !refreshFailed ? 'PASS' : 'UNKNOWN');
      setMessage(action === 'prepare'
        ? `Worker: ${result.worker?.status || 'UNKNOWN'}; อนุมัติ Live: ${result.worker?.mainnetLiveApproved === true ? 'true' : 'UNKNOWN'}; จำนวนครั้งส่งออร์เดอร์ ${result.worker?.orderSubmissionAttempts ?? 'UNKNOWN'}`
        : action === 'start'
          ? `เริ่มระบบแล้ว: ${result.worker?.status || 'UNKNOWN'}; จำนวนครั้งส่งออร์เดอร์ที่ตรวจกลับได้ ${result.worker?.orderSubmissionAttempts ?? 'UNKNOWN'}`
          : `ผล ${action}: ${next?.status || result.status || result.evidence_status || 'UNKNOWN'}`);
      if (refreshFailed) setMessage('Action ตอบกลับแล้ว แต่การอ่านสถานะล่าสุดล้มเหลว; ตรวจ Worker ก่อนดำเนินการต่อ');
    } catch (error) {
      setEvidence('FAIL');
      const err = error as any;
      const data = err?.data;
      const reason = data?.reason;
      const code = data?.error;
      if (reason && code) {
        setMessage(`${code} · ${reason}`);
      } else {
        setMessage(error instanceof Error ? error.message : 'ตรวจสอบไม่สำเร็จ');
      }
    } finally {
      setBusy(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!campaign?.campaignId) return;
    setBusy(true);
    setMessage('กำลังอ่านสถานะแคมเปญ');
    setEvidence('UNKNOWN');
    try {
      const result = await apiClient.get<PilotResponse>(`/api/local/pilot/${encodeURIComponent(campaign.campaignId)}`);
      const next = campaignFrom(result);
      if (next) setCampaign(next);
      setAccounting(result.accounting);
      setReadiness(result.readiness);
      setPreparation(result.preparation);
      setRuntime(result.runtime);
      setEvidence(result.evidence_status === 'VERIFIED' ? 'PASS' : 'UNKNOWN');
      setMessage(`สถานะแคมเปญ: ${next?.status || 'UNKNOWN'}`);
    } catch (error) {
      setEvidence('FAIL');
      const err = error as any;
      const data = err?.data;
      const reason = data?.reason;
      const code = data?.error;
      if (reason && code) {
        setMessage(`${code} · ${reason}`);
      } else {
        setMessage(error instanceof Error ? error.message : 'อ่านสถานะไม่สำเร็จ');
      }
    } finally {
      setBusy(false);
    }
  }, [campaign?.campaignId]);

  return (
    <section className="rounded-xl border border-amber-500/30 bg-zinc-950/70 p-5 space-y-4" aria-labelledby="pilot-title">
      <div className="flex items-start gap-3">
        <ShieldAlert className="mt-0.5 h-5 w-5 shrink-0 text-amber-400" aria-hidden="true" />
        <div>
          <h3 id="pilot-title" className="text-sm font-semibold text-zinc-100">Local Live Research Pilot</h3>
          <p className="mt-1 text-xs leading-5 text-zinc-400">
            ETHUSDC Futures · QUICK เท่านั้น · 7 วัน · notional สูงสุด 50 USDC · planned risk 2 USDC · drawdown stop 5 USDC · leverage ไม่เกิน 10x
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-red-500/30 bg-red-950/20 p-3 text-xs leading-5 text-red-100">
        <div className="flex gap-2 font-semibold"><AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
          คำสั่ง Start อาจเปิดความเสี่ยงด้วยเงินจริง
        </div>
        <p className="mt-1 text-red-200/80">วงเงินเป็นเกณฑ์สั่งหยุด ไม่ใช่การรับประกันขาดทุนสูงสุด หากข้อมูลหรือการยืนยันสถานะไม่ครบ ระบบต้องหยุดเพิ่มความเสี่ยง</p>
      </div>

      <div className="rounded-lg border border-amber-500/30 bg-amber-950/20 p-3 text-xs leading-5 text-amber-100">
        Readiness: {readiness?.status || 'NOT_RUN'} · {readiness?.blockers?.join(', ') || 'ยังไม่มีหลักฐาน'}
        <div className="mt-1">Prepare: {preparation?.status || 'NOT_RUN'} · Worker: {runtime && runtime !== 'UNKNOWN'
          ? `${runtime.executionMode || 'UNKNOWN'}/${runtime.engineState || 'UNKNOWN'} · MAINNET_LIVE_APPROVED=${runtime.mainnetLiveApproved === true ? 'true' : 'UNKNOWN'}`
          : 'UNKNOWN'}</div>
      </div>

      <div className="grid gap-3 sm:grid-cols-[1fr_auto]">
        <label className="text-xs text-zinc-300">
          กลยุทธ์ที่ผูกกับแคมเปญ (เปลี่ยนภายหลังไม่ได้)
          <select value={strategyId} disabled={busy || Boolean(campaign)} onChange={(event) => setStrategyId(event.target.value as LocalLivePilotStrategyId)}
            className="mt-1 block w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100">
            {STRATEGIES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
        </label>
        <button type="button" disabled={busy || Boolean(campaign)} onClick={() => void runAction('request', { strategyId })}
          className="self-end rounded-md bg-cyan-700 px-4 py-2 text-xs font-semibold text-white disabled:opacity-50">
          สร้างคำขออนุมัติ
        </button>
      </div>

      {campaign && <div className="rounded-lg border border-zinc-800 bg-zinc-900/60 p-3 text-xs">
        <div className="grid gap-2 sm:grid-cols-2">
          <div><span className="text-zinc-500">Campaign</span><div className="mt-0.5 break-all font-mono text-zinc-200">{campaign.campaignId}</div></div>
          <div><span className="text-zinc-500">สถานะ</span><div className="mt-0.5 text-zinc-200">{campaign.status}</div></div>
          <div><span className="text-zinc-500">กลยุทธ์</span><div className="mt-0.5 text-zinc-200">{campaign.strategyId.toUpperCase()}</div></div>
          <div><span className="text-zinc-500">หมดอายุ</span><div className="mt-0.5 text-zinc-200">{campaign.campaignExpiresAt ? new Date(campaign.campaignExpiresAt).toLocaleString() : 'UNKNOWN'}</div></div>
        </div>
        <div className="mt-3 grid gap-2 border-t border-zinc-800 pt-3 sm:grid-cols-3">
          {[
            ['Net PnL', accounting?.netPnlUsdc],
            ['Peak PnL', accounting?.peakNetPnlUsdc],
            ['Drawdown', accounting?.drawdownUsdc],
            ['Realized / unrealized', accounting?.realizedPnlUsdc && accounting?.unrealizedPnlUsdc
              ? `${accounting.realizedPnlUsdc} / ${accounting.unrealizedPnlUsdc}` : undefined],
            ['Fees / funding', accounting?.feesUsdc && accounting?.fundingUsdc
              ? `${accounting.feesUsdc} / ${accounting.fundingUsdc}` : undefined],
            ['Slippage', accounting?.slippageUsdc],
          ].map(([label, value]) => <div key={label}>
            <div className="text-zinc-500">{label} (USDC)</div>
            <div className={`mt-0.5 ${accounting?.status === 'VERIFIED' ? 'text-emerald-300' : 'text-amber-300'}`}>
              {accounting?.status === 'VERIFIED' && value && value !== 'UNKNOWN'
                ? value
                : `UNKNOWN — ${accounting?.reason || accounting?.status || 'ยังไม่มีหลักฐานบัญชีที่อ่านกลับ'}`}
            </div>
          </div>)}
        </div>
        {accounting?.lastEventAt && <p className="mt-2 text-[11px] text-zinc-500">ข้อมูลบัญชีล่าสุด: {new Date(accounting.lastEventAt).toLocaleString()}</p>}
      </div>}

      <div className="flex flex-wrap gap-2">
        {campaign?.status === 'PENDING_APPROVAL' && <button type="button" disabled={busy || readiness?.canApprove !== true}
          onClick={() => void runAction('approve', { campaignId: campaign.campaignId })}
          className="rounded-md bg-amber-600 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">อนุมัติแคมเปญ</button>}
        {campaign && ['APPROVED', 'ACTIVE'].includes(campaign.status) && <button type="button" disabled={busy || readiness?.canApprove !== true || preparation?.status === 'PASS'}
          onClick={() => void runAction('prepare', { campaignId: campaign.campaignId })}
          className="rounded-md bg-cyan-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">เตรียม LIVE/DISARMED</button>}
        {campaign && ['APPROVED', 'ACTIVE'].includes(campaign.status) && <button type="button"
          disabled={busy || readiness?.canStart !== true || preparation?.status !== 'PASS'}
          onClick={() => void runAction('start', { campaignId: campaign.campaignId })}
          className="rounded-md bg-red-700 px-3 py-2 text-xs font-semibold text-white disabled:opacity-50">เริ่ม Live Pilot (ARM)</button>}
        {campaign && <button type="button" disabled={busy} onClick={() => void refresh()}
          className="rounded-md border border-zinc-700 px-3 py-2 text-xs text-zinc-200 disabled:opacity-50"><Activity className="mr-1 inline h-3.5 w-3.5" />อ่านสถานะ</button>}
        {campaign && ['APPROVED', 'ACTIVE'].includes(campaign.status) && <button type="button" disabled={busy} onClick={() => void runAction('close-only', { campaignId: campaign.campaignId })}
          className="rounded-md border border-amber-500/50 px-3 py-2 text-xs text-amber-200 disabled:opacity-50">หยุดเพิ่มความเสี่ยง</button>}
        {campaign && !['COMPLETED', 'REVOKED'].includes(campaign.status) && <button type="button" disabled={busy} onClick={() => void runAction('revoke', { campaignId: campaign.campaignId })}
          className="rounded-md border border-red-500/50 px-3 py-2 text-xs text-red-200 disabled:opacity-50">เพิกถอน</button>}
      </div>

      <div role="status" aria-live="polite" className="flex items-center gap-2 border-t border-zinc-800 pt-3 text-xs text-zinc-400">
        {busy ? <LoaderCircle className="h-4 w-4 animate-spin" aria-hidden="true" />
          : evidence === 'PASS' ? <CheckCircle2 className="h-4 w-4 text-emerald-400" aria-hidden="true" />
            : evidence === 'FAIL' ? <AlertTriangle className="h-4 w-4 text-red-400" aria-hidden="true" /> : null}
        <span>หลักฐาน: {evidence} · {message}</span>
      </div>
    </section>
  );
};
