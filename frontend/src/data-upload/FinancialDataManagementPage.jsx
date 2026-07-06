import React, { useCallback, useRef, useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger, } from '@/components/ui/tabs';
import { Alert, AlertDescription, AlertTitle, } from '@/components/ui/alert';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, } from '@/components/ui/table';
import {
    AlertCircle,
    CheckCircle2,
    DollarSign,
    Eye,
    FileText,
    Info,
    Loader2,
    Minus,
    TrendingDown,
    TrendingUp,
    Upload,
    X,
} from 'lucide-react';
import { toast } from 'sonner';

// ─── CSV Templates ────────────────────────────────────────────────────────────

const BUDGET_ACTUALS_TEMPLATE = `year,category_id,category_name,planned,actual,variance,previous_actual
2026,101,Accountant - Professional Fees,1182.00,0.00,1182.00,1168.84
2026,102,Accounting Service Provision,616.00,165.38,450.62,653.67
2026,110,Cleaning,27500.00,4520.73,22979.27,27738.00
2026,123,Insurance Premiums,37500.00,6691.73,30808.27,6746.27
2026,128,Management Fee,27682.00,7411.80,20270.20,29294.25
2026,136,Water - Utility,37797.00,0.00,37797.00,34360.93`;

const BUDGET_CATEGORIES_TEMPLATE = `year,fund_type,name,budgeted_amount,actual_amount,description
2026,administrative,Insurance,85000,84500,Building and public liability
2026,administrative,Cleaning,42000,,Common area cleaning
2026,sinking,Lift Replacement,45000,,Scheduled for Q3`;

const ACTUALS_TEMPLATE = `year,fund_type,name,actual_amount
2026,administrative,Insurance,84500
2026,administrative,Cleaning,41200
2026,sinking,Lift Replacement,44800`;

const UNIT_LEDGER_TEMPLATE = `year,lot_number,unit_number,uoe,property_type,admin_opening,admin_levied,admin_paid,admin_closing,sinking_opening,sinking_levied,sinking_paid,sinking_closing,total_levied,total_paid,net_balance
2026,LOT1,UA001,115,apartment,0,365.14,365.14,0,0,103.23,103.23,0,468.37,468.37,0
2026,LOT71,TH001,149,townhouse,0,473.42,0,473.42,0,133.75,0,133.75,607.17,0,607.17`;

// ─── Helpers ──────────────────────────────────────────────────────────────────

const fmt = (n) =>
    typeof n === 'number'
        ? n.toLocaleString('en-AU', {minimumFractionDigits: 2, maximumFractionDigits: 2})
        : n;

const fmtAUD = (n) => `$${fmt(n)}`;

const parseFloat2 = (s) => {
    const v = parseFloat(String(s).replace(/[$,]/g, ''));
    return isNaN(v) ? 0 : v;
};

const statusBadge = (status) => {
    if (status === 'over_budget') return <Badge variant="destructive" className="text-xs">Over Budget</Badge>;
    if (status === 'under_budget') return <Badge variant="outline"
                                                 className="text-xs text-emerald-600 border-emerald-300">Under
        Budget</Badge>;
    return <Badge variant="secondary" className="text-xs">On Track</Badge>;
};

const varianceIcon = (variance) => {
    if (variance > 100) return <TrendingDown className="h-3 w-3 text-emerald-500 inline mr-1"/>;
    if (variance < -100) return <TrendingUp className="h-3 w-3 text-red-500 inline mr-1"/>;
    return <Minus className="h-3 w-3 text-muted-foreground inline mr-1"/>;
};

// ─── Parse rich CSV client-side ───────────────────────────────────────────────

function parseRichCsv(text) {
    const lines = text.split('\n').map((l) => l.trim()).filter(Boolean);
    if (lines.length < 2) return {rows: [], errors: ['File contains no data rows']};

    const header = lines[ 0 ].split(',').map((h) => h.trim().toLowerCase());
    const required = ['year', 'category_name', 'planned'];
    const missing = required.filter((r) => !header.includes(r));
    if (missing.length) return {rows: [], errors: [`Missing columns: ${missing.join(', ')}`]};

    const get = (parts, col) => {
        const idx = header.indexOf(col);
        return idx >= 0 ? ( parts[ idx ] || '' ).trim().replace(/[$,]/g, '') : '';
    };

    const rows = [];
    const errors = [];

    for (let i = 1; i < lines.length; i++) {
        const parts = lines[ i ].split(',');
        const year = get(parts, 'year');
        const name = get(parts, 'category_name');
        if (!year || !name) {
            errors.push(`Row ${i + 1}: missing year or category_name`);
            continue;
        }
        const planned = parseFloat2(get(parts, 'planned'));
        const actual = parseFloat2(get(parts, 'actual') || '0');
        const prevActual = parseFloat2(get(parts, 'previous_actual') || '0');
        const varianceRaw = get(parts, 'variance');
        const variance = varianceRaw !== '' ? parseFloat2(varianceRaw) : parseFloat2(( planned - actual ).toFixed(2));
        const catId = get(parts, 'category_id');

        let status = 'on_track';
        if (planned > 0 && actual > planned) status = 'over_budget';
        else if (actual < planned) status = 'under_budget';

        rows.push({
            year,
            category_id: catId,
            category_name: name,
            planned,
            actual,
            variance,
            previous_actual: prevActual,
            status
        });
    }

    return {rows, errors};
}

// ─── DropZone component ───────────────────────────────────────────────────────

const DropZone = ({file, setFile, setResult}) => {
    const [dragOver, setDragOver] = useState(false);
    const inputRef = useRef(null);

    const handleDrop = useCallback((e) => {
        e.preventDefault();
        setDragOver(false);
        const dropped = e.dataTransfer.files[ 0 ];
        if (dropped && dropped.name.endsWith('.csv')) {
            setFile(dropped);
            setResult(null);
        } else {
            toast.error('Please drop a CSV file');
        }
    }, [setFile, setResult]);

    return (
        <div
            className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
                dragOver ? 'border-primary bg-primary/5' : 'border-muted-foreground/25 hover:border-primary/50'
            }`}
            onDragOver={(e) => {
                e.preventDefault();
                setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={handleDrop}
            onClick={() => inputRef.current?.click()}
        >
            <Upload className="h-8 w-8 text-muted-foreground mx-auto mb-3"/>
            {file ? (
                <div className="flex items-center justify-center gap-2">
                    <FileText className="h-4 w-4 text-primary"/>
                    <span className="font-medium">{file.name}</span>
                    <span className="text-sm text-muted-foreground">({( file.size / 1024 ).toFixed(1)} KB)</span>
                    <button
                        onClick={(e) => {
                            e.stopPropagation();
                            setFile(null);
                            setResult(null);
                        }}
                        className="ml-1 text-muted-foreground hover:text-destructive"
                    >
                        <X className="h-3 w-3"/>
                    </button>
                </div>
            ) : (
                <>
                    <p className="text-sm text-muted-foreground">Drag and drop your CSV file here, or click to
                        browse</p>
                    <p className="text-xs text-muted-foreground mt-1">Only .csv files accepted</p>
                </>
            )}
            <input ref={inputRef} type="file" accept=".csv" className="hidden"
                   onChange={(e) => {
                       const f = e.target.files[ 0 ];
                       if (f) {
                           setFile(f);
                           setResult(null);
                       }
                   }}/>
        </div>
    );
};

// ─── SimpleUploadSection (existing budget categories / actuals / unit ledger) ─

const SimpleUploadSection = ({
                                 title,
                                 description,
                                 endpoint,
                                 template,
                                 templateFilename,
                                 acceptedFields,
                                 extraFields
                             }) => {
    const {api} = useAuth();
    const [file, setFile] = useState(null);
    const [uploading, setUploading] = useState(false);
    const [result, setResult] = useState(null);

    const downloadTemplate = () => {
        const blob = new Blob([template], {type: 'text/csv'});
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = templateFilename;
        a.click();
        window.URL.revokeObjectURL(url);
    };

    const handleUpload = async () => {
        if (!file) return;
        setUploading(true);
        setResult(null);
        try {
            const formData = new FormData();
            formData.append('file', file);
            if (extraFields) Object.entries(extraFields).forEach(([k, v]) => formData.append(k, v));
            const res = await api.post(endpoint, formData, {headers: {'Content-Type': 'multipart/form-data'}});
            setResult({success: true, data: res.data});
            toast.success(res.data.message || 'Upload successful');
        } catch (err) {
            const msg = err.response?.data?.detail || 'Upload failed';
            setResult({success: false, error: msg});
            toast.error(msg);
        } finally {
            setUploading(false);
        }
    };

    return (
        <Card className="card-dashboard">
            <CardHeader>
                <div className="flex items-start justify-between">
                    <div>
                        <CardTitle>{title}</CardTitle>
                        <CardDescription className="mt-1">{description}</CardDescription>
                    </div>
                    <Button variant="outline" size="sm" onClick={downloadTemplate}>
                        <FileText className="mr-2 h-4 w-4"/>Download Template
                    </Button>
                </div>
            </CardHeader>
            <CardContent className="space-y-4">
                <Alert>
                    <Info className="h-4 w-4"/>
                    <AlertTitle>Required CSV columns</AlertTitle>
                    <AlertDescription className="font-mono text-xs mt-1">{acceptedFields}</AlertDescription>
                </Alert>
                <DropZone file={file} setFile={setFile} setResult={setResult}/>
                <Button onClick={handleUpload} disabled={!file || uploading} className="w-full">
                    {uploading ? <><Loader2 className="mr-2 h-4 w-4 animate-spin"/>Uploading…</> : <><Upload
                        className="mr-2 h-4 w-4"/>Upload CSV</>}
                </Button>
                {result && (
                    <Alert variant={result.success ? 'default' : 'destructive'}>
                        {result.success ? <CheckCircle2 className="h-4 w-4"/> : <AlertCircle className="h-4 w-4"/>}
                        <AlertTitle>{result.success ? 'Upload Successful' : 'Upload Failed'}</AlertTitle>
                        <AlertDescription>
                            {result.success ? (
                                <div className="text-sm space-y-1">
                                    {result.data?.message && <p>{result.data.message}</p>}
                                    {result.data?.imported !== undefined && (
                                        <p>{result.data.imported} records
                                            imported, {result.data?.skipped || 0} skipped, {result.data?.errors?.length || 0} errors</p>
                                    )}
                                    {result.data?.errors?.length > 0 && (
                                        <ul className="mt-2 text-xs list-disc ml-4 space-y-0.5">
                                            {result.data.errors.slice(0, 5).map((e, i) => <li key={i}>{e}</li>)}
                                            {result.data.errors.length > 5 &&
                                                <li>…and {result.data.errors.length - 5} more</li>}
                                        </ul>
                                    )}
                                </div>
                            ) : <p className="text-sm">{result.error}</p>}
                        </AlertDescription>
                    </Alert>
                )}
            </CardContent>
        </Card>
    );
};

// ─── Rich Budget+Actuals Upload (with preview) ────────────────────────────────

const RichBudgetActualsSection = () => {
    const {api} = useAuth();
    const [file, setFile] = useState(null);
    const [preview, setPreview] = useState(null);
    const [parseErrors, setParseErrors] = useState([]);
    const [uploading, setUploading] = useState(false);
    const [result, setResult] = useState(null);

    const downloadTemplate = () => {
        const blob = new Blob([BUDGET_ACTUALS_TEMPLATE], {type: 'text/csv'});
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'budget_actuals_template.csv';
        a.click();
        window.URL.revokeObjectURL(url);
    };

    const handleFileChange = (newFile) => {
        setFile(newFile);
        setPreview(null);
        setParseErrors([]);
        setResult(null);
        if (!newFile) return;
        const reader = new FileReader();
        reader.onload = (e) => {
            const {rows, errors} = parseRichCsv(e.target.result);
            setPreview(rows);
            setParseErrors(errors);
        };
        reader.readAsText(newFile);
    };

    const handleUpload = async () => {
        if (!file || !preview?.length) return;
        setUploading(true);
        setResult(null);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const res = await api.post('/finance/upload-budget-actuals', formData, {
                headers: {'Content-Type': 'multipart/form-data'},
            });
            setResult({success: true, data: res.data});
            toast.success(res.data.message || 'Budget actuals uploaded');
        } catch (err) {
            const msg = err.response?.data?.detail || 'Upload failed';
            setResult({success: false, error: msg});
            toast.error(msg);
        } finally {
            setUploading(false);
        }
    };

    // Summaries
    const totalPlanned = preview?.reduce((s, r) => s + r.planned, 0) ?? 0;
    const totalActual = preview?.reduce((s, r) => s + r.actual, 0) ?? 0;
    const overBudget = preview?.filter((r) => r.status === 'over_budget').length ?? 0;

    return (
        <Card className="card-dashboard">
            <CardHeader>
                <div className="flex items-start justify-between">
                    <div>
                        <CardTitle>Upload Budget + Actuals (Combined)</CardTitle>
                        <CardDescription className="mt-1">
                            Import the full budget vs actuals report from your strata accounting system. Includes
                            planned, YTD actuals, variance, and prior-year figures in one file.
                        </CardDescription>
                    </div>
                    <Button variant="outline" size="sm" onClick={downloadTemplate}>
                        <FileText className="mr-2 h-4 w-4"/>Download Template
                    </Button>
                </div>
            </CardHeader>
            <CardContent className="space-y-4">
                <Alert>
                    <Info className="h-4 w-4"/>
                    <AlertTitle>CSV format — header row required</AlertTitle>
                    <AlertDescription className="font-mono text-xs mt-1">
                        year, category_id, category_name, planned, actual, variance, previous_actual
                        <br/>
                        <span
                            className="text-muted-foreground">• category_id 101–199 → Administrative fund &nbsp;|&nbsp; 200–299 → Sinking fund</span>
                        <br/>
                        <span
                            className="text-muted-foreground">• Amounts: numeric, no $ or commas &nbsp;|&nbsp; Positive variance = under budget (unspent)</span>
                    </AlertDescription>
                </Alert>

                <DropZone file={file} setFile={handleFileChange} setResult={setResult}/>

                {/* Parse errors */}
                {parseErrors.length > 0 && (
                    <Alert variant="destructive">
                        <AlertCircle className="h-4 w-4"/>
                        <AlertTitle>Parse warnings ({parseErrors.length})</AlertTitle>
                        <AlertDescription>
                            <ul className="text-xs list-disc ml-4 mt-1 space-y-0.5">
                                {parseErrors.map((e, i) => <li key={i}>{e}</li>)}
                            </ul>
                        </AlertDescription>
                    </Alert>
                )}

                {/* Preview table */}
                {preview && preview.length > 0 && !result && (
                    <div className="space-y-3">
                        <div className="flex items-center justify-between">
                            <div className="flex items-center gap-2">
                                <Eye className="h-4 w-4 text-primary"/>
                                <span className="font-medium text-sm">Preview — {preview.length} categories</span>
                            </div>
                            <div className="flex gap-4 text-sm text-muted-foreground">
                                <span>Planned: <strong
                                    className="text-foreground">{fmtAUD(totalPlanned)}</strong></span>
                                <span>Actual YTD: <strong
                                    className="text-foreground">{fmtAUD(totalActual)}</strong></span>
                                {overBudget > 0 && (
                                    <Badge variant="destructive" className="text-xs">{overBudget} over budget</Badge>
                                )}
                            </div>
                        </div>

                        <div className="border rounded-lg overflow-auto max-h-80">
                            <Table>
                                <TableHeader>
                                    <TableRow>
                                        <TableHead className="w-8">#</TableHead>
                                        <TableHead>Category</TableHead>
                                        <TableHead className="text-right">Planned</TableHead>
                                        <TableHead className="text-right">Actual YTD</TableHead>
                                        <TableHead className="text-right">Variance</TableHead>
                                        <TableHead className="text-right">Prev Year</TableHead>
                                        <TableHead>Status</TableHead>
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {preview.map((row, i) => (
                                        <TableRow key={i}
                                                  className={row.status === 'over_budget' ? 'bg-red-50 dark:bg-red-950/20' : ''}>
                                            <TableCell
                                                className="text-xs text-muted-foreground">{row.category_id || i + 1}</TableCell>
                                            <TableCell className="text-sm font-medium">{row.category_name}</TableCell>
                                            <TableCell
                                                className="text-right text-sm font-mono">{fmtAUD(row.planned)}</TableCell>
                                            <TableCell
                                                className="text-right text-sm font-mono">{fmtAUD(row.actual)}</TableCell>
                                            <TableCell className="text-right text-sm font-mono">
                                                {varianceIcon(row.variance)}{fmtAUD(Math.abs(row.variance))}
                                            </TableCell>
                                            <TableCell
                                                className="text-right text-sm font-mono text-muted-foreground">{fmtAUD(row.previous_actual)}</TableCell>
                                            <TableCell>{statusBadge(row.status)}</TableCell>
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </div>

                        <Alert>
                            <Info className="h-4 w-4"/>
                            <AlertDescription className="text-sm">
                                Review the data above. Existing categories for the same year will
                                be <strong>updated</strong>. New categories will be <strong>created</strong>.
                            </AlertDescription>
                        </Alert>

                        <Button onClick={handleUpload} disabled={uploading} className="w-full"
                                data-testid="confirm-upload-btn">
                            {uploading ? (
                                <><Loader2
                                    className="mr-2 h-4 w-4 animate-spin"/>Importing {preview.length} categories…</>
                            ) : (
                                <><CheckCircle2 className="mr-2 h-4 w-4"/>Confirm Import
                                    — {preview.length} categories</>
                            )}
                        </Button>
                    </div>
                )}

                {/* Result */}
                {result && (
                    <Alert variant={result.success ? 'default' : 'destructive'}>
                        {result.success ? <CheckCircle2 className="h-4 w-4"/> : <AlertCircle className="h-4 w-4"/>}
                        <AlertTitle>{result.success ? 'Import Successful' : 'Import Failed'}</AlertTitle>
                        <AlertDescription>
                            {result.success ? (
                                <div className="text-sm space-y-1">
                                    <p>{result.data?.message}</p>
                                    {result.data?.errors?.length > 0 && (
                                        <ul className="mt-2 text-xs list-disc ml-4">
                                            {result.data.errors.slice(0, 5).map((e, i) => <li key={i}>{e}</li>)}
                                        </ul>
                                    )}
                                </div>
                            ) : <p className="text-sm">{result.error}</p>}
                        </AlertDescription>
                    </Alert>
                )}
            </CardContent>
        </Card>
    );
};

// ─── Fund Balances Form ───────────────────────────────────────────────────────

const FundBalancesSection = () => {
    const {api} = useAuth();
    const today = new Date().toISOString().split('T')[ 0 ];
    const currentYear = new Date().getFullYear();
    const financialYear = `${currentYear}-${currentYear + 1}`;

    const [form, setForm] = useState({
        admin_balance: '',
        sinking_balance: '',
        as_of_date: today,
        financial_year: financialYear,
        notes: '',
    });
    const [submitting, setSubmitting] = useState(false);
    const [result, setResult] = useState(null);

    const set = (field, value) => setForm((f) => ( {...f, [ field ]: value} ));

    const totalBalance = ( parseFloat(form.admin_balance) || 0 ) + ( parseFloat(form.sinking_balance) || 0 );

    const handleSubmit = async (e) => {
        e.preventDefault();
        if (!form.admin_balance || !form.sinking_balance || !form.as_of_date || !form.financial_year) {
            toast.error('All fields except Notes are required');
            return;
        }
        setSubmitting(true);
        setResult(null);
        try {
            const formData = new FormData();
            Object.entries(form).forEach(([k, v]) => formData.append(k, v));
            const res = await api.post('/finance/fund-balances', formData, {
                headers: {'Content-Type': 'multipart/form-data'},
            });
            setResult({success: true, data: res.data});
            toast.success(`Fund balances recorded — Total: $${res.data.total?.toLocaleString('en-AU', {minimumFractionDigits: 2})}`);
        } catch (err) {
            const msg = err.response?.data?.detail || 'Failed to save fund balances';
            setResult({success: false, error: msg});
            toast.error(msg);
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <Card className="card-dashboard">
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <DollarSign className="h-5 w-5 text-primary"/>
                    Update Fund Balances
                </CardTitle>
                <CardDescription>
                    Record the current bank account balances for the Admin Fund and Sinking Fund. This creates a
                    reconciliation snapshot and updates the dashboard balance cards.
                </CardDescription>
            </CardHeader>
            <CardContent>
                <form onSubmit={handleSubmit} className="space-y-5">
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        {/* Admin Fund */}
                        <div className="space-y-2">
                            <Label htmlFor="admin_balance">
                                Admin Fund Balance <span className="text-destructive">*</span>
                            </Label>
                            <div className="relative">
                                <span
                                    className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground text-sm">$</span>
                                <Input
                                    id="admin_balance"
                                    type="number"
                                    step="0.01"
                                    min="0"
                                    placeholder="9187.44"
                                    className="pl-7"
                                    value={form.admin_balance}
                                    onChange={(e) => set('admin_balance', e.target.value)}
                                    data-testid="admin-balance-input"
                                    required
                                />
                            </div>
                            <p className="text-xs text-muted-foreground">As shown on the admin fund bank statement</p>
                        </div>

                        {/* Sinking Fund */}
                        <div className="space-y-2">
                            <Label htmlFor="sinking_balance">
                                Sinking Fund Balance <span className="text-destructive">*</span>
                            </Label>
                            <div className="relative">
                                <span
                                    className="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground text-sm">$</span>
                                <Input
                                    id="sinking_balance"
                                    type="number"
                                    step="0.01"
                                    min="0"
                                    placeholder="193337.03"
                                    className="pl-7"
                                    value={form.sinking_balance}
                                    onChange={(e) => set('sinking_balance', e.target.value)}
                                    data-testid="sinking-balance-input"
                                    required
                                />
                            </div>
                            <p className="text-xs text-muted-foreground">As shown on the sinking fund bank statement</p>
                        </div>

                        {/* As of Date */}
                        <div className="space-y-2">
                            <Label htmlFor="as_of_date">
                                Statement Date <span className="text-destructive">*</span>
                            </Label>
                            <Input
                                id="as_of_date"
                                type="date"
                                value={form.as_of_date}
                                onChange={(e) => set('as_of_date', e.target.value)}
                                data-testid="as-of-date-input"
                                required
                            />
                        </div>

                        {/* Financial Year */}
                        <div className="space-y-2">
                            <Label htmlFor="financial_year">
                                Financial Year <span className="text-destructive">*</span>
                            </Label>
                            <Input
                                id="financial_year"
                                placeholder="2026-2027"
                                value={form.financial_year}
                                onChange={(e) => set('financial_year', e.target.value)}
                                data-testid="financial-year-input"
                                required
                            />
                            <p className="text-xs text-muted-foreground">Format: YYYY-YYYY (e.g. 2026-2027)</p>
                        </div>

                        {/* Notes */}
                        <div className="space-y-2 md:col-span-2">
                            <Label htmlFor="notes">Notes (optional)</Label>
                            <Input
                                id="notes"
                                placeholder="e.g. Statement date — March 2026 bank reconciliation"
                                value={form.notes}
                                onChange={(e) => set('notes', e.target.value)}
                            />
                        </div>
                    </div>

                    {/* Live total preview */}
                    {( form.admin_balance || form.sinking_balance ) && (
                        <div className="grid grid-cols-3 gap-3 p-4 rounded-lg bg-muted/40 border">
                            <div className="text-center">
                                <p className="text-xs text-muted-foreground">Admin Fund</p>
                                <p className="text-lg font-semibold text-primary">
                                    {fmtAUD(parseFloat(form.admin_balance) || 0)}
                                </p>
                            </div>
                            <div className="text-center">
                                <p className="text-xs text-muted-foreground">Sinking Fund</p>
                                <p className="text-lg font-semibold text-primary">
                                    {fmtAUD(parseFloat(form.sinking_balance) || 0)}
                                </p>
                            </div>
                            <div className="text-center border-l">
                                <p className="text-xs text-muted-foreground">Total</p>
                                <p className="text-lg font-bold">{fmtAUD(totalBalance)}</p>
                            </div>
                        </div>
                    )}

                    <Button type="submit" disabled={submitting} className="w-full" data-testid="save-fund-balances-btn">
                        {submitting ? (
                            <><Loader2 className="mr-2 h-4 w-4 animate-spin"/>Saving…</>
                        ) : (
                            <><DollarSign className="mr-2 h-4 w-4"/>Save Fund Balances</>
                        )}
                    </Button>
                </form>

                {result && (
                    <Alert variant={result.success ? 'default' : 'destructive'} className="mt-4">
                        {result.success ? <CheckCircle2 className="h-4 w-4"/> : <AlertCircle className="h-4 w-4"/>}
                        <AlertTitle>{result.success ? 'Fund Balances Saved' : 'Save Failed'}</AlertTitle>
                        <AlertDescription>
                            {result.success ? (
                                <div className="text-sm space-y-1">
                                    <p>Admin Fund: <strong>{fmtAUD(result.data.admin_balance)}</strong></p>
                                    <p>Sinking Fund: <strong>{fmtAUD(result.data.sinking_balance)}</strong></p>
                                    <p>Total: <strong>{fmtAUD(result.data.total)}</strong></p>
                                    <p className="text-muted-foreground text-xs">As
                                        of {result.data.as_of_date} — {result.data.financial_year}</p>
                                </div>
                            ) : <p className="text-sm">{result.error}</p>}
                        </AlertDescription>
                    </Alert>
                )}
            </CardContent>
        </Card>
    );
};

// ─── Main Page ────────────────────────────────────────────────────────────────

const FinancialDataManagementPage = () => {
    const {user} = useAuth();

    const allowedRoles = ['super_admin', 'strata_manager'];
    if (!allowedRoles.includes(user?.role)) {
        return (
            <div className="py-16 text-center space-y-3">
                <AlertCircle className="h-12 w-12 text-muted-foreground/40 mx-auto"/>
                <p className="font-medium">Access Restricted</p>
                <p className="text-sm text-muted-foreground">
                    This page is available to Super Admin, Strata Manager, and Chairman only.
                </p>
            </div>
        );
    }

    return (
        <div className="space-y-6" data-testid="financial-data-management-page">
            <div>
                <h1 className="text-2xl font-bold">Financial Data Management</h1>
                <p className="text-muted-foreground mt-1">
                    Import budget categories, actuals, unit levy data, and record current fund balances.
                </p>
            </div>

            <Alert>
                <Info className="h-4 w-4"/>
                <AlertTitle>Upload Guidelines</AlertTitle>
                <AlertDescription>
                    <ul className="text-sm list-disc ml-4 mt-1 space-y-1">
                        <li>Files must be UTF-8 CSV with the exact header columns shown per tab.</li>
                        <li>Year format: 4-digit calendar year (e.g. <code>2026</code>). Financial year
                            format: <code>2026-2027</code>.
                        </li>
                        <li>Fund type: <code>administrative</code> or <code>sinking</code>. Category IDs 101–199 =
                            admin, 200–299 = sinking.
                        </li>
                        <li>Amounts: numeric, no $ signs or commas (e.g. <code>85000.00</code>). Negative =
                            credit/income.
                        </li>
                        <li>Existing records for the same year + category name will be <strong>updated</strong> (upsert
                            — safe to re-run).
                        </li>
                    </ul>
                </AlertDescription>
            </Alert>

            <Tabs defaultValue="rich-upload">
                <TabsList className="grid w-full grid-cols-4">
                    <TabsTrigger value="rich-upload" data-testid="tab-rich-upload">
                        <TrendingUp className="mr-1.5 h-4 w-4"/>
                        Budget + Actuals
                    </TabsTrigger>
                    <TabsTrigger value="fund-balances" data-testid="tab-fund-balances">
                        <DollarSign className="mr-1.5 h-4 w-4"/>
                        Fund Balances
                    </TabsTrigger>
                    <TabsTrigger value="unit-ledger" data-testid="tab-unit-ledger">
                        <FileText className="mr-1.5 h-4 w-4"/>
                        Unit Ledger
                    </TabsTrigger>
                    <TabsTrigger value="advanced" data-testid="tab-advanced">
                        <Upload className="mr-1.5 h-4 w-4"/>
                        Advanced
                    </TabsTrigger>
                </TabsList>

                {/* Tab 1: Rich Budget + Actuals (new combined format) */}
                <TabsContent value="rich-upload" className="mt-4">
                    <RichBudgetActualsSection/>
                </TabsContent>

                {/* Tab 2: Fund Balances */}
                <TabsContent value="fund-balances" className="mt-4">
                    <FundBalancesSection/>
                </TabsContent>

                {/* Tab 3: Unit Ledger */}
                <TabsContent value="unit-ledger" className="mt-4">
                    <SimpleUploadSection
                        title="Upload Unit Levy Ledger"
                        description="Import per-unit levy ledger from your strata system export. Creates or updates unit_levy_ledger records for opening balances, levied, paid, and closing amounts."
                        endpoint="/finance/upload-unit-ledger"
                        template={UNIT_LEDGER_TEMPLATE}
                        templateFilename="unit_ledger_template.csv"
                        acceptedFields="year, lot_number, unit_number, uoe, property_type, admin_opening, admin_levied, admin_paid, admin_closing, sinking_opening, sinking_levied, sinking_paid, sinking_closing, total_levied, total_paid, net_balance"
                    />
                    <Card className="card-dashboard mt-4">
                        <CardHeader>
                            <CardTitle className="text-base">Unit Number Mapping</CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="grid grid-cols-2 gap-4 text-sm">
                                <div>
                                    <p className="font-medium mb-2">Apartments</p>
                                    <p className="text-muted-foreground">LOT1–LOT70 → UA001–UA070</p>
                                </div>
                                <div>
                                    <p className="font-medium mb-2">Townhouses</p>
                                    <p className="text-muted-foreground">LOT71–LOT87 → TH001–TH017</p>
                                </div>
                            </div>
                            <p className="text-xs text-muted-foreground mt-3">
                                The import script auto-derives unit_number from lot_number if unit_number is not
                                provided.
                            </p>
                        </CardContent>
                    </Card>
                </TabsContent>

                {/* Tab 4: Advanced (separate budget categories + actuals) */}
                <TabsContent value="advanced" className="mt-4 space-y-6">
                    <Alert>
                        <Info className="h-4 w-4"/>
                        <AlertTitle>Advanced — Separate Uploads</AlertTitle>
                        <AlertDescription className="text-sm">
                            Use these if you need to upload budget proposals and year-end actuals separately. The
                            "Budget + Actuals" tab is recommended for combined imports from your strata accounting
                            system.
                        </AlertDescription>
                    </Alert>

                    <SimpleUploadSection
                        title="Upload Budget Categories (Proposed)"
                        description="Import proposed budget for a new levy year. Sets the budgeted_amount for each expense line item."
                        endpoint="/finance/upload-budget-categories"
                        template={BUDGET_CATEGORIES_TEMPLATE}
                        templateFilename="budget_categories_template.csv"
                        acceptedFields="year, fund_type, name, budgeted_amount, actual_amount (optional), description (optional)"
                    />

                    <SimpleUploadSection
                        title="Upload Year-End Actuals"
                        description="Update actual expense amounts for existing budget lines. Matches on year + fund_type + category name."
                        endpoint="/finance/upload-actuals"
                        template={ACTUALS_TEMPLATE}
                        templateFilename="actuals_template.csv"
                        acceptedFields="year, fund_type, name, actual_amount"
                    />
                </TabsContent>
            </Tabs>
        </div>
    );
};

export default FinancialDataManagementPage;
