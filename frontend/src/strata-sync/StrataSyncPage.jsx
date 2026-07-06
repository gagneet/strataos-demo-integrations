import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
    AlertCircle,
    CheckCircle2,
    Clock,
    Database,
    DollarSign,
    Eye,
    Info,
    KeyRound,
    Lock,
    RefreshCw,
    Shield,
    TrendingDown,
    TrendingUp,
    Users,
    XCircle,
} from 'lucide-react';
import { toast } from 'sonner';

// ─── Error guidance ───────────────────────────────────────────────────────────

function errorGuidance(error) {
    if (!error) return null;
    const e = error.toLowerCase();
    if (e.includes('incorrect username') || e.includes('incorrect password') || e.includes('invalid credential'))
        return 'Your portal credentials appear to be wrong. Check PORTAL_EMAIL and PORTAL_PASSWORD in backend/.env then restart the backend.';
    if (e.includes('portal_email') || e.includes('portal_password'))
        return 'Portal credentials are not set. Add PORTAL_EMAIL and PORTAL_PASSWORD to backend/.env and restart the backend.';
    if (e.includes('pin timeout') || e.includes('no pin entered'))
        return 'No PIN was entered within 5 minutes. Start a new sync and enter the PIN from your email promptly.';
    if (e.includes('pin input'))
        return 'The portal login page structure may have changed. Contact your administrator.';
    if (e.includes('timeout') || e.includes('timed out'))
        return 'The portal took too long to respond. Check your server internet connection and try again.';
    if (e.includes('executable') || e.includes('playwright'))
        return 'Browser not installed. Run: cd backend && venv/bin/python -m playwright install chromium';
    return null;
}

// ─── Status meta ──────────────────────────────────────────────────────────────

const STATUS_META = {
    starting: {label: 'Logging in…', color: 'bg-blue-500', icon: Lock, spinning: true},
    waiting_pin: {label: 'Enter PIN', color: 'bg-amber-500', icon: KeyRound, spinning: false},
    scraping: {label: 'Scraping data…', color: 'bg-blue-500', icon: Database, spinning: true},
    cleaning: {label: 'Cleaning data…', color: 'bg-blue-500', icon: RefreshCw, spinning: true},
    preview: {label: 'Review before saving', color: 'bg-purple-500', icon: Eye, spinning: false},
    syncing: {label: 'Saving to system…', color: 'bg-blue-500', icon: Database, spinning: true},
    complete: {label: 'Sync complete', color: 'bg-green-500', icon: CheckCircle2, spinning: false},
    error: {label: 'Error', color: 'bg-red-500', icon: XCircle, spinning: false},
    cancelled: {label: 'Cancelled', color: 'bg-gray-400', icon: XCircle, spinning: false},
};

const ACTIVE_STATES = new Set(['starting', 'waiting_pin', 'scraping', 'cleaning', 'preview', 'syncing']);

// ─── Risk badge ───────────────────────────────────────────────────────────────

function RiskBadge({level}) {
    const map = {
        LOW: 'bg-green-100 text-green-800',
        MEDIUM: 'bg-amber-100 text-amber-800',
        HIGH: 'bg-red-100 text-red-800'
    };
    return <span
        className={`px-2 py-0.5 rounded-full text-xs font-semibold ${map[ level ] || 'bg-gray-100 text-gray-700'}`}>{level}</span>;
}

// ─── Building Health Card ─────────────────────────────────────────────────────

function HealthCard({summary}) {
    if (!summary) return null;

    const synced = new Date(summary.generated_at).toLocaleString('en-AU', {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit',
    });

    return (
        <div className="space-y-6">
            {/* Overview row */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <MetricCard
                    icon={<Shield className="w-5 h-5 text-blue-600"/>}
                    label="Risk Level"
                    value={<RiskBadge level={summary.risk_level}/>}
                />
                <MetricCard
                    icon={<TrendingUp className="w-5 h-5 text-green-600"/>}
                    label="Collection Rate"
                    value={`${summary.collection_rate}%`}
                    sub={`${summary.clear_count} lots clear`}
                />
                <MetricCard
                    icon={<TrendingDown className="w-5 h-5 text-red-600"/>}
                    label="Total Arrears"
                    value={`$${summary.arrears_total?.toLocaleString('en-AU', {minimumFractionDigits: 2})}`}
                    sub={`${summary.arrears_count} lots`}
                />
                <MetricCard
                    icon={<DollarSign className="w-5 h-5 text-indigo-600"/>}
                    label="Total Credits"
                    value={`$${summary.credit_total?.toLocaleString('en-AU', {minimumFractionDigits: 2})}`}
                    sub={`${summary.credit_count} lots`}
                />
            </div>

            {/* Top arrears */}
            {summary.top_arrears?.length > 0 && (
                <div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-2">
                        <Users className="w-4 h-4"/> Top Arrears
                    </h3>
                    <div className="rounded-lg border overflow-hidden">
                        <table className="w-full text-sm">
                            <thead className="bg-gray-50">
                            <tr>
                                <th className="text-left px-4 py-2 font-medium text-gray-600">Unit</th>
                                <th className="text-left px-4 py-2 font-medium text-gray-600">Owner</th>
                                <th className="text-right px-4 py-2 font-medium text-gray-600">Owing</th>
                            </tr>
                            </thead>
                            <tbody className="divide-y">
                            {summary.top_arrears.map((row, i) => (
                                <tr key={i} className="hover:bg-gray-50">
                                    <td className="px-4 py-2 font-mono text-xs">{row.unit_number}</td>
                                    <td className="px-4 py-2">{row.owner}</td>
                                    <td className="px-4 py-2 text-right font-medium text-red-600">
                                        ${row.balance?.toLocaleString('en-AU', {minimumFractionDigits: 2})}
                                    </td>
                                </tr>
                            ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Budget overruns */}
            {summary.budget_overruns?.length > 0 && (
                <div>
                    <h3 className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-2">
                        <AlertCircle className="w-4 h-4 text-amber-500"/> Budget Overruns
                    </h3>
                    <div className="rounded-lg border overflow-hidden">
                        <table className="w-full text-sm">
                            <thead className="bg-gray-50">
                            <tr>
                                <th className="text-left px-4 py-2 font-medium text-gray-600">Category</th>
                                <th className="text-left px-4 py-2 font-medium text-gray-600">Fund</th>
                                <th className="text-right px-4 py-2 font-medium text-gray-600">Overspend</th>
                                <th className="text-right px-4 py-2 font-medium text-gray-600">%</th>
                            </tr>
                            </thead>
                            <tbody className="divide-y">
                            {summary.budget_overruns.map((row, i) => (
                                <tr key={i} className="hover:bg-gray-50">
                                    <td className="px-4 py-2">{row.category}</td>
                                    <td className="px-4 py-2">
                                        <Badge variant="outline" className="text-xs">{row.fund}</Badge>
                                    </td>
                                    <td className="px-4 py-2 text-right font-medium text-orange-600">
                                        ${row.overspend?.toLocaleString('en-AU', {minimumFractionDigits: 2})}
                                    </td>
                                    <td className="px-4 py-2 text-right text-red-600">+{row.overspend_pct?.toFixed(1)}%</td>
                                </tr>
                            ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            )}

            {/* Fund totals */}
            <div className="grid grid-cols-2 gap-4">
                <FundCard label="Admin Fund" data={summary.admin_fund}/>
                <FundCard label="Sinking Fund" data={summary.capital_works_fund}/>
            </div>

            <p className="text-xs text-gray-400 text-right">Last synced: {synced}</p>
        </div>
    );
}

function MetricCard({icon, label, value, sub}) {
    return (
        <div className="border rounded-xl p-4 bg-white shadow-sm flex flex-col gap-1">
            <div className="flex items-center gap-2 text-gray-500 text-xs">{icon}{label}</div>
            <div className="text-xl font-bold text-gray-900">{value}</div>
            {sub && <div className="text-xs text-gray-400">{sub}</div>}
        </div>
    );
}

function FundCard({label, data}) {
    if (!data) return null;
    const variance = data.total_actual - data.total_planned;
    return (
        <div className="border rounded-xl p-4 bg-white shadow-sm">
            <p className="text-sm font-semibold text-gray-700 mb-2">{label}</p>
            <div className="space-y-1 text-sm">
                <Row label="Planned"
                     value={`$${data.total_planned?.toLocaleString('en-AU', {minimumFractionDigits: 2})}`}/>
                <Row label="Actual"
                     value={`$${data.total_actual?.toLocaleString('en-AU', {minimumFractionDigits: 2})}`}/>
                <Row
                    label="Variance"
                    value={`${variance >= 0 ? '+' : ''}$${variance.toLocaleString('en-AU', {minimumFractionDigits: 2})}`}
                    valueClass={variance < 0 ? 'text-red-600 font-medium' : 'text-green-600 font-medium'}
                />
            </div>
        </div>
    );
}

function Row({label, value, valueClass = 'text-gray-800'}) {
    return (
        <div className="flex justify-between">
            <span className="text-gray-500">{label}</span>
            <span className={valueClass}>{value}</span>
        </div>
    );
}

// ─── Step indicator ───────────────────────────────────────────────────────────

const STEPS = [
    {id: 'starting', label: 'Login'},
    {id: 'waiting_pin', label: 'PIN'},
    {id: 'scraping', label: 'Scrape'},
    {id: 'cleaning', label: 'Clean'},
    {id: 'preview', label: 'Review'},
    {id: 'syncing', label: 'Save'},
    {id: 'complete', label: 'Done'},
];

function StepBar({status}) {
    const idx = STEPS.findIndex(s => s.id === status);
    return (
        <div className="flex items-center gap-1">
            {STEPS.map((step, i) => {
                const done = i < idx || status === 'complete';
                const active = i === idx && status !== 'complete';
                return (
                    <React.Fragment key={step.id}>
                        <div className="flex flex-col items-center gap-1">
                            <div className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all
                ${done ? 'bg-green-500 text-white' : active ? 'bg-blue-600 text-white ring-2 ring-blue-200' : 'bg-gray-200 text-gray-400'}`}>
                                {done ? '✓' : i + 1}
                            </div>
                            <span
                                className={`text-[10px] ${active ? 'text-blue-600 font-semibold' : done ? 'text-green-600' : 'text-gray-400'}`}>
                {step.label}
              </span>
                        </div>
                        {i < STEPS.length - 1 && (
                            <div className={`flex-1 h-0.5 mb-4 ${done ? 'bg-green-500' : 'bg-gray-200'}`}/>
                        )}
                    </React.Fragment>
                );
            })}
        </div>
    );
}

// ─── Preview panel ────────────────────────────────────────────────────────────

function fmtMoney(n) {
    if (n == null) return '—';
    const abs = Math.abs(n);
    const str = abs.toLocaleString('en-AU', {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return n < 0 ? `(${str})` : str;
}

function PreviewPanel({previewData, onConfirm, onDiscard, loading}) {
    if (!previewData) return null;
    const {financials = [], owners = [], bank_accounts = [], summary} = previewData;
    const adminItems = financials.filter(f => f.fund === 'admin');
    const sinkingItems = financials.filter(f => f.fund === 'capital_works');
    const inArrears = owners.filter(o => o.status === 'ARREARS');
    const inCredit = owners.filter(o => o.status === 'CREDIT');

    const statusPill = (s) => {
        const map = {
            ARREARS: 'bg-red-100 text-red-700',
            CREDIT: 'bg-green-100 text-green-700',
            CLEAR: 'bg-gray-100 text-gray-500'
        };
        return <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${map[ s ] || ''}`}>{s}</span>;
    };

    const FinTable = ({items, label}) => (
        <div>
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">{label}</p>
            <div className="rounded-lg border overflow-hidden">
                <table className="w-full text-xs">
                    <thead className="bg-gray-50">
                    <tr>
                        <th className="text-left px-3 py-2 font-medium text-gray-600">Category</th>
                        <th className="text-right px-3 py-2 font-medium text-gray-600">Planned</th>
                        <th className="text-right px-3 py-2 font-medium text-gray-600">Actual</th>
                        <th className="text-right px-3 py-2 font-medium text-gray-600">Variance</th>
                    </tr>
                    </thead>
                    <tbody className="divide-y">
                    {items.map((r, i) => (
                        <tr key={i} className="hover:bg-gray-50">
                            <td className="px-3 py-1.5">{r.category}</td>
                            <td className="px-3 py-1.5 text-right font-mono">${fmtMoney(r.planned)}</td>
                            <td className="px-3 py-1.5 text-right font-mono">${fmtMoney(r.actual)}</td>
                            <td className={`px-3 py-1.5 text-right font-mono ${r.variance < 0 ? 'text-red-600' : 'text-green-600'}`}>
                                ${fmtMoney(r.variance)}
                            </td>
                        </tr>
                    ))}
                    </tbody>
                    <tfoot className="bg-gray-50 font-semibold">
                    <tr>
                        <td className="px-3 py-2 text-gray-700">Total</td>
                        <td className="px-3 py-2 text-right font-mono">${fmtMoney(items.reduce((s, r) => s + r.planned, 0))}</td>
                        <td className="px-3 py-2 text-right font-mono">${fmtMoney(items.reduce((s, r) => s + r.actual, 0))}</td>
                        <td className="px-3 py-2 text-right font-mono">${fmtMoney(items.reduce((s, r) => s + r.variance, 0))}</td>
                    </tr>
                    </tfoot>
                </table>
            </div>
        </div>
    );

    return (
        <div className="space-y-4">
            <div className="flex items-center gap-2 p-3 rounded-lg bg-purple-50 border border-purple-200">
                <Eye className="w-4 h-4 text-purple-600 flex-shrink-0"/>
                <p className="text-sm text-purple-800 font-medium">
                    Review the scraped data below. Confirm to write it to the database, or discard to cancel.
                </p>
            </div>

            <Tabs defaultValue="admin">
                <TabsList className="grid w-full grid-cols-4">
                    <TabsTrigger value="admin">Admin Fund ({adminItems.length})</TabsTrigger>
                    <TabsTrigger value="sinking">Sinking Fund ({sinkingItems.length})</TabsTrigger>
                    <TabsTrigger value="owners">Owners ({owners.length})</TabsTrigger>
                    <TabsTrigger value="banks">Banks ({bank_accounts.length})</TabsTrigger>
                </TabsList>

                <TabsContent value="admin" className="mt-3 max-h-72 overflow-y-auto">
                    <FinTable items={adminItems} label="Administrative Fund Expenses"/>
                </TabsContent>

                <TabsContent value="sinking" className="mt-3 max-h-72 overflow-y-auto">
                    <FinTable items={sinkingItems} label="Sinking Fund"/>
                </TabsContent>

                <TabsContent value="owners" className="mt-3">
                    <div className="flex gap-3 mb-3 text-xs">
                        <span className="text-red-600 font-medium">{inArrears.length} in arrears</span>
                        <span className="text-green-600 font-medium">{inCredit.length} in credit</span>
                        <span
                            className="text-gray-500">{owners.length - inArrears.length - inCredit.length} clear</span>
                    </div>
                    <div className="rounded-lg border overflow-hidden max-h-72 overflow-y-auto">
                        <table className="w-full text-xs">
                            <thead className="bg-gray-50 sticky top-0">
                            <tr>
                                <th className="text-left px-3 py-2 font-medium text-gray-600">Unit</th>
                                <th className="text-left px-3 py-2 font-medium text-gray-600">Owner</th>
                                <th className="text-right px-3 py-2 font-medium text-gray-600">UoE</th>
                                <th className="text-right px-3 py-2 font-medium text-gray-600">Balance</th>
                                <th className="text-center px-3 py-2 font-medium text-gray-600">Status</th>
                            </tr>
                            </thead>
                            <tbody className="divide-y">
                            {owners.map((o, i) => (
                                <tr key={i} className="hover:bg-gray-50">
                                    <td className="px-3 py-1.5 font-mono">{o.unit_number}</td>
                                    <td className="px-3 py-1.5 truncate max-w-[200px]">{o.owner}</td>
                                    <td className="px-3 py-1.5 text-right">{o.uoe}</td>
                                    <td className={`px-3 py-1.5 text-right font-mono ${o.balance > 0 ? 'text-red-600' : o.balance < 0 ? 'text-green-600' : 'text-gray-500'}`}>
                                        ${fmtMoney(Math.abs(o.balance))}
                                    </td>
                                    <td className="px-3 py-1.5 text-center">{statusPill(o.status)}</td>
                                </tr>
                            ))}
                            </tbody>
                        </table>
                    </div>
                </TabsContent>

                <TabsContent value="banks" className="mt-3">
                    {bank_accounts.length === 0 ? (
                        <p className="text-sm text-gray-400 text-center py-6">No bank accounts detected on this report
                            page.</p>
                    ) : (
                        <div className="rounded-lg border overflow-hidden">
                            <table className="w-full text-xs">
                                <thead className="bg-gray-50">
                                <tr>
                                    <th className="text-left px-3 py-2 font-medium text-gray-600">BSB</th>
                                    <th className="text-left px-3 py-2 font-medium text-gray-600">Account No.</th>
                                    <th className="text-right px-3 py-2 font-medium text-gray-600">Admin</th>
                                    <th className="text-right px-3 py-2 font-medium text-gray-600">Sinking</th>
                                    <th className="text-right px-3 py-2 font-medium text-gray-600">Total</th>
                                </tr>
                                </thead>
                                <tbody className="divide-y">
                                {bank_accounts.map((a, i) => (
                                    <tr key={i}>
                                        <td className="px-3 py-2 font-mono">{a.bsb}</td>
                                        <td className="px-3 py-2 font-mono">{a.account_number}</td>
                                        <td className="px-3 py-2 text-right font-mono">${fmtMoney(a.admin_balance)}</td>
                                        <td className="px-3 py-2 text-right font-mono">${fmtMoney(a.sinking_balance)}</td>
                                        <td className="px-3 py-2 text-right font-mono font-semibold">${fmtMoney(a.total_balance)}</td>
                                    </tr>
                                ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </TabsContent>
            </Tabs>

            {summary && (
                <div className="grid grid-cols-3 gap-3 p-3 rounded-lg bg-gray-50 border text-xs">
                    <div className="text-center"><p className="text-gray-500">Collection Rate</p><p
                        className="font-bold text-lg">{summary.collection_rate}%</p></div>
                    <div className="text-center"><p className="text-gray-500">Total Arrears</p><p
                        className="font-bold text-lg text-red-600">${fmtMoney(summary.arrears_total)}</p></div>
                    <div className="text-center"><p className="text-gray-500">Risk Level</p><RiskBadge
                        level={summary.risk_level}/></div>
                </div>
            )}

            <div className="flex gap-3 pt-2 border-t">
                <Button
                    onClick={onConfirm}
                    disabled={!!loading}
                    className="bg-green-600 hover:bg-green-700 text-white"
                >
                    {loading === 'confirm'
                        ? <RefreshCw className="w-4 h-4 animate-spin mr-2"/>
                        : <CheckCircle2 className="w-4 h-4 mr-2"/>}
                    Confirm Upload
                </Button>
                <Button
                    variant="outline"
                    onClick={onDiscard}
                    disabled={!!loading}
                    className="border-red-300 text-red-600 hover:bg-red-50"
                >
                    {loading === 'discard'
                        ? <RefreshCw className="w-4 h-4 animate-spin mr-2"/>
                        : <XCircle className="w-4 h-4 mr-2"/>}
                    Discard
                </Button>
            </div>
        </div>
    );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export default function StrataSyncPage() {
    const {api} = useAuth();

    const [jobId, setJobId] = useState(null);
    const [job, setJob] = useState(null);
    const [pin, setPin] = useState('');
    const [pinLoading, setPinLoading] = useState(false);
    const [starting, setStarting] = useState(false);
    const [lastSync, setLastSync] = useState(null);
    const [summary, setSummary] = useState(null);
    const [activityLog, setActivityLog] = useState([]);
    const [previewLoading, setPreviewLoading] = useState(null); // 'confirm' | 'discard' | null

    const pollRef = useRef(null);
    const lastMsgRef = useRef(null);
    const logEndRef = useRef(null);

    // ── Load last sync on mount ──────────────────────────────────────────────
    useEffect(() => {
        api.get('/strata/sync/latest')
            .then(r => {
                setLastSync(r.data.last_job);
                setSummary(r.data.summary);
            })
            .catch(() => {
            });
    }, [api]);

    // ── Poll job status ──────────────────────────────────────────────────────
    const pollStatus = useCallback((id) => {
        pollRef.current = setInterval(async () => {
            try {
                const r = await api.get(`/strata/sync/status/${id}`);
                setJob(r.data);

                // Append to activity log whenever the message changes
                const msg = r.data.message || r.data.error;
                if (msg && msg !== lastMsgRef.current) {
                    lastMsgRef.current = msg;
                    setActivityLog(prev => [
                        ...prev,
                        {
                            time: new Date().toLocaleTimeString('en-AU', {
                                hour: '2-digit',
                                minute: '2-digit',
                                second: '2-digit'
                            }),
                            status: r.data.status,
                            text: msg,
                        },
                    ]);
                    setTimeout(() => logEndRef.current?.scrollIntoView({behavior: 'smooth'}), 50);
                }

                if (!ACTIVE_STATES.has(r.data.status)) {
                    clearInterval(pollRef.current);
                    if (r.data.status === 'complete') {
                        toast.success('Strata portal sync completed successfully');
                        setSummary(r.data.result);
                        setLastSync(r.data);
                    } else if (r.data.status === 'cancelled') {
                        toast.info('Sync cancelled — no data was written.');
                    } else if (r.data.status === 'error') {
                        toast.error(`Sync failed: ${r.data.error}`);
                    }
                }
            } catch {
                clearInterval(pollRef.current);
            }
        }, 3000);
    }, [api]);

    useEffect(() => () => clearInterval(pollRef.current), []);

    // ── Preview confirm / discard ────────────────────────────────────────────
    const handleConfirm = async () => {
        setPreviewLoading('confirm');
        try {
            await api.post('/strata/sync/preview/confirm', {job_id: jobId, action: 'confirm'});
            toast.success('Confirmed — saving data to the system…');
        } catch (err) {
            toast.error(err.response?.data?.detail || 'Failed to confirm preview');
        } finally {
            setPreviewLoading(null);
        }
    };

    const handleDiscard = async () => {
        setPreviewLoading('discard');
        try {
            await api.post('/strata/sync/preview/confirm', {job_id: jobId, action: 'discard'});
            toast.info('Data discarded — nothing was written to the system.');
        } catch (err) {
            toast.error(err.response?.data?.detail || 'Failed to discard preview');
        } finally {
            setPreviewLoading(null);
        }
    };

    // ── Start sync ───────────────────────────────────────────────────────────
    const handleStart = async () => {
        setStarting(true);
        setJob(null);
        setPin('');
        setActivityLog([]);
        lastMsgRef.current = null;
        try {
            const r = await api.post('/strata/sync/start');
            setJobId(r.data.job_id);
            setJob({status: 'starting', message: 'Initialising...'});
            pollStatus(r.data.job_id);
        } catch (err) {
            toast.error(err.response?.data?.detail || 'Failed to start sync');
        } finally {
            setStarting(false);
        }
    };

    // ── Submit PIN ───────────────────────────────────────────────────────────
    const handlePin = async () => {
        if (!pin.trim()) return;
        setPinLoading(true);
        try {
            await api.post('/strata/sync/pin', {job_id: jobId, pin: pin.trim()});
            setPin('');
            toast.success('PIN submitted — scraping in progress…');
        } catch (err) {
            toast.error(err.response?.data?.detail || 'Failed to submit PIN');
        } finally {
            setPinLoading(false);
        }
    };

    const isActive = job && ACTIVE_STATES.has(job.status);
    const meta = job ? STATUS_META[ job.status ] : null;
    const StatusIcon = meta?.icon;

    return (
        <div className="max-w-3xl mx-auto p-6 space-y-6">

            {/* Header */}
            <div>
                <h1 className="text-2xl font-bold text-gray-900">Strata Portal Sync</h1>
                <p className="text-sm text-gray-500 mt-1">
                    Pulls the latest financial data and owner levy positions from the strata management portal.
                </p>
            </div>

            {/* Trigger card */}
            <Card>
                <CardHeader className="pb-3">
                    <CardTitle className="text-base flex items-center gap-2">
                        <RefreshCw className="w-4 h-4"/> Sync Now
                    </CardTitle>
                </CardHeader>
                <CardContent className="space-y-5">

                    {/* Step bar — only shown while a job is running */}
                    {isActive && <StepBar status={job.status}/>}

                    {/* Status message */}
                    {job && (
                        <div className={`flex items-center gap-3 p-4 rounded-lg ${
                            job.status === 'complete' ? 'bg-green-50 border border-green-200' :
                                job.status === 'error' ? 'bg-red-50 border border-red-200' :
                                    'bg-blue-50 border border-blue-200'
                        }`}>
                            {StatusIcon && (
                                <StatusIcon className={`w-5 h-5 flex-shrink-0 ${meta.spinning ? 'animate-spin' : ''} ${
                                    job.status === 'complete' ? 'text-green-600' :
                                        job.status === 'error' ? 'text-red-600' : 'text-blue-600'
                                }`}/>
                            )}
                            <div>
                                <p className="text-sm font-medium text-gray-900">{meta?.label}</p>
                                <p className="text-xs text-gray-500">{job.message}</p>
                            </div>
                        </div>
                    )}

                    {/* Error detail + actionable guidance */}
                    {job?.status === 'error' && job.error && (
                        <div className="space-y-2">
                            <Alert variant="destructive">
                                <XCircle className="w-4 h-4"/>
                                <AlertDescription className="text-xs font-mono break-all">{job.error}</AlertDescription>
                            </Alert>
                            {errorGuidance(job.error) && (
                                <Alert className="border-amber-200 bg-amber-50">
                                    <Info className="w-4 h-4 text-amber-600"/>
                                    <AlertDescription
                                        className="text-xs text-amber-800">{errorGuidance(job.error)}</AlertDescription>
                                </Alert>
                            )}
                        </div>
                    )}

                    {/* PIN input — shown only when job is waiting */}
                    {job?.status === 'waiting_pin' && (
                        <div className="space-y-3">
                            <p className="text-sm text-amber-700 font-medium">
                                A one-time PIN has been sent to your registered email address. Enter it below to
                                continue.
                            </p>
                            <div className="flex gap-2">
                                <Input
                                    value={pin}
                                    onChange={e => setPin(e.target.value)}
                                    onKeyDown={e => e.key === 'Enter' && handlePin()}
                                    placeholder="Enter PIN from email"
                                    maxLength={12}
                                    className="font-mono text-lg tracking-widest max-w-[220px]"
                                    autoFocus
                                />
                                <Button onClick={handlePin} disabled={!pin.trim() || pinLoading}>
                                    {pinLoading ? <RefreshCw className="w-4 h-4 animate-spin"/> : 'Submit'}
                                </Button>
                            </div>
                            <p className="text-xs text-gray-400">
                                No email received? Check your spam/junk folder — the PIN comes from the Strata Web portal.
                                The PIN expires after a few minutes; if it has expired, cancel and start a new sync.
                            </p>
                        </div>
                    )}

                    {/* Preview panel — shown when scraper is waiting for confirmation */}
                    {job?.status === 'preview' && job?.preview_data && (
                        <PreviewPanel
                            previewData={job.preview_data}
                            onConfirm={handleConfirm}
                            onDiscard={handleDiscard}
                            loading={previewLoading}
                        />
                    )}

                    {/* Activity log — shown while running or after completion/error */}
                    {activityLog.length > 0 && (
                        <div>
                            <p className="text-xs font-semibold text-gray-500 mb-1 uppercase tracking-wide">Activity
                                Log</p>
                            <div
                                className="bg-gray-950 rounded-lg p-3 max-h-44 overflow-y-auto font-mono text-xs space-y-1">
                                {activityLog.map((entry, i) => (
                                    <div key={i} className="flex gap-2">
                                        <span className="text-gray-500 shrink-0">{entry.time}</span>
                                        <span className={
                                            entry.status === 'error' ? 'text-red-400' :
                                                entry.status === 'complete' ? 'text-green-400' :
                                                    entry.status === 'waiting_pin' ? 'text-amber-400' :
                                                        'text-blue-300'
                                        }>{entry.text}</span>
                                    </div>
                                ))}
                                <div ref={logEndRef}/>
                            </div>
                        </div>
                    )}

                    {/* Start button */}
                    <Button
                        onClick={handleStart}
                        disabled={starting || isActive}
                        className="w-full"
                        size="lg"
                    >
                        {starting || isActive ? (
                            <RefreshCw className="w-4 h-4 animate-spin mr-2"/>
                        ) : (
                            <RefreshCw className="w-4 h-4 mr-2"/>
                        )}
                        {isActive ? 'Sync in progress…' : 'Sync Strata Portal Data'}
                    </Button>

                    {lastSync && !isActive && (
                        <p className="text-xs text-center text-gray-400 flex items-center justify-center gap-1">
                            <Clock className="w-3 h-3"/>
                            Last synced {new Date(lastSync.started_at).toLocaleString('en-AU')}
                        </p>
                    )}
                </CardContent>
            </Card>

            {/* Building Health Card */}
            {summary && (
                <Card>
                    <CardHeader className="pb-3">
                        <CardTitle className="text-base flex items-center gap-2">
                            <Shield className="w-4 h-4"/> Building Financial Health
                        </CardTitle>
                    </CardHeader>
                    <CardContent>
                        <HealthCard summary={summary}/>
                    </CardContent>
                </Card>
            )}

            {/* Empty state */}
            {!summary && !isActive && (
                <div className="text-center py-12 text-gray-400">
                    <Database className="w-10 h-10 mx-auto mb-3 opacity-30"/>
                    <p className="text-sm">No sync data yet. Run a sync to populate the building health card.</p>
                </div>
            )}
        </div>
    );
}
