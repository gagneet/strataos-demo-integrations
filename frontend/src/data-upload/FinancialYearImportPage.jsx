import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow, } from '@/components/ui/table';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue, } from '@/components/ui/select';
import {
    AlertCircle,
    ArrowLeft,
    ArrowRight,
    BarChart3,
    CheckCircle,
    ChevronRight,
    Download,
    FileText,
    History,
    Loader2,
    PieChart,
    RefreshCw,
    TrendingUp,
    Upload,
    Users,
} from 'lucide-react';
import { toast } from 'sonner';

// ─── CSV Type Configuration ───────────────────────────────────────────────────

const CSV_TYPES = {
    unit_owners: {
        label: 'Unit & Owner Details',
        description: 'Upload unit numbers, owner names (primary & secondary), UOE values, unit types, and occupancy status',
        icon: Users,
        endpoint: 'unit-owners',
        templateType: 'unit_owners',
        templateName: 'unit_owners_template.csv',
        columns: [
            {name: 'lot_number', required: true, example: 'LOT1', desc: 'Lot number (e.g., LOT1)'},
            {name: 'unit_number', required: true, example: 'UA001', desc: 'Unit number (e.g., UA001, TH001)'},
            {name: 'unit_type', required: true, example: 'apartment', desc: 'apartment | townhouse | villa'},
            {name: 'mixed_use_type', required: false, example: 'townhouse', desc: 'Additional type descriptor'},
            {name: 'primary_owner_name', required: true, example: 'John Smith', desc: 'Primary owner full name'},
            {
                name: 'secondary_owner_name',
                required: false,
                example: 'Jane Smith',
                desc: 'Secondary/joint owner full name'
            },
            {name: 'owner_email', required: false, example: 'john@example.com', desc: 'Owner email address'},
            {name: 'uoe', required: true, example: '115', desc: 'Unit of entitlement (integer, sum = 10000)'},
            {name: 'asset_value', required: false, example: '650000', desc: 'Current asset value in AUD'},
            {
                name: 'status',
                required: false,
                example: 'owner_occupied',
                desc: 'owner_occupied | tenanted | vacant | investment'
            },
            {name: 'notes', required: false, example: '', desc: 'Additional notes'},
        ],
    },
    annual_levy: {
        label: 'Annual Levy Summary',
        description: 'Upload proposed and actual levy rates, fund totals, opening/closing balances for a financial year',
        icon: TrendingUp,
        endpoint: 'annual-levy',
        templateType: 'annual_levy',
        templateName: 'annual_levy_template.csv',
        columns: [
            {name: 'financial_year', required: true, example: '2026', desc: 'Financial year (e.g., 2026)'},
            {
                name: 'admin_levy_per_uoe_proposed',
                required: true,
                example: '23.45',
                desc: 'Proposed admin levy per UOE (annual)'
            },
            {
                name: 'admin_levy_per_uoe_actual',
                required: false,
                example: '23.45',
                desc: 'Actual admin levy per UOE (end of year)'
            },
            {
                name: 'sinking_levy_per_uoe_proposed',
                required: true,
                example: '6.72',
                desc: 'Proposed sinking levy per UOE (annual)'
            },
            {
                name: 'sinking_levy_per_uoe_actual',
                required: false,
                example: '6.72',
                desc: 'Actual sinking levy per UOE (end of year)'
            },
            {
                name: 'admin_total_income_proposed',
                required: true,
                example: '340870.20',
                desc: 'Admin fund total income proposed'
            },
            {
                name: 'admin_total_income_actual',
                required: false,
                example: '338500.00',
                desc: 'Admin fund total income actual'
            },
            {
                name: 'admin_total_expenses_proposed',
                required: true,
                example: '340870.20',
                desc: 'Admin fund total expenses proposed'
            },
            {
                name: 'admin_total_expenses_actual',
                required: false,
                example: '335200.00',
                desc: 'Admin fund actual expenses'
            },
            {name: 'admin_opening_balance', required: true, example: '15000.00', desc: 'Admin fund opening balance'},
            {
                name: 'admin_closing_balance_projected',
                required: true,
                example: '15000.00',
                desc: 'Admin fund projected closing balance'
            },
            {
                name: 'admin_closing_balance_actual',
                required: false,
                example: '18300.00',
                desc: 'Admin fund actual closing balance'
            },
            {
                name: 'sinking_total_income_proposed',
                required: true,
                example: '99504.90',
                desc: 'Sinking fund total income proposed'
            },
            {
                name: 'sinking_total_income_actual',
                required: false,
                example: '99000.00',
                desc: 'Sinking fund total income actual'
            },
            {
                name: 'sinking_total_expenses_proposed',
                required: true,
                example: '45000.00',
                desc: 'Sinking fund total expenses proposed'
            },
            {
                name: 'sinking_total_expenses_actual',
                required: false,
                example: '42500.00',
                desc: 'Sinking fund actual expenses'
            },
            {
                name: 'sinking_opening_balance',
                required: true,
                example: '85000.00',
                desc: 'Sinking fund opening balance'
            },
            {
                name: 'sinking_closing_balance_projected',
                required: true,
                example: '139504.90',
                desc: 'Sinking fund projected closing balance'
            },
            {
                name: 'sinking_closing_balance_actual',
                required: false,
                example: '141500.00',
                desc: 'Sinking fund actual closing balance'
            },
        ],
    },
    budget_categories: {
        label: 'Budget Categories',
        description: 'Upload expense categories with proposed budgeted amounts and actual spent amounts',
        icon: PieChart,
        endpoint: 'budget-categories',
        templateType: 'budget_categories',
        templateName: 'budget_categories_template.csv',
        columns: [
            {name: 'financial_year', required: true, example: '2026', desc: 'Financial year'},
            {name: 'fund_type', required: true, example: 'admin', desc: 'admin | sinking | administrative'},
            {name: 'category_name', required: true, example: 'Management Fee', desc: 'Budget category name'},
            {name: 'budgeted_amount', required: true, example: '27682.00', desc: 'Proposed budget amount in AUD'},
            {
                name: 'actual_amount',
                required: false,
                example: '27682.00',
                desc: 'Actual spent amount (leave blank if not yet available)'
            },
            {
                name: 'description',
                required: false,
                example: 'Annual strata management fee',
                desc: 'Category description'
            },
        ],
    },
    unit_levy_status: {
        label: 'Per-Unit Levy Status',
        description: 'Upload per-unit quarterly levy amounts, payments, arrears and status for a financial year',
        icon: BarChart3,
        endpoint: 'unit-levy-status',
        templateType: 'unit_levy_status',
        templateName: 'unit_levy_status_template.csv',
        columns: [
            {name: 'lot_number', required: true, example: 'LOT1', desc: 'Lot number'},
            {name: 'unit_number', required: true, example: 'UA001', desc: 'Unit number'},
            {name: 'financial_year', required: true, example: '2026', desc: 'Financial year'},
            {name: 'admin_opening_balance', required: false, example: '0.00', desc: 'Admin fund opening balance'},
            {name: 'admin_levied', required: true, example: '2345.00', desc: 'Total admin levy charged for year'},
            {name: 'admin_paid', required: true, example: '2345.00', desc: 'Total admin levy paid'},
            {name: 'admin_closing_balance', required: false, example: '0.00', desc: 'Admin fund closing balance'},
            {name: 'sinking_opening_balance', required: false, example: '0.00', desc: 'Sinking fund opening balance'},
            {name: 'sinking_levied', required: true, example: '672.00', desc: 'Total sinking levy charged'},
            {name: 'sinking_paid', required: true, example: '672.00', desc: 'Total sinking levy paid'},
            {name: 'sinking_closing_balance', required: false, example: '0.00', desc: 'Sinking fund closing balance'},
            {
                name: 'levy_status',
                required: false,
                example: 'current',
                desc: 'current | arrears | credit | partial | prepaid | overdue'
            },
            {name: 'q1_amount', required: false, example: '756.75', desc: 'Q1 levy amount charged'},
            {name: 'q1_paid', required: false, example: '756.75', desc: 'Q1 amount paid'},
            {name: 'q1_date', required: false, example: '2026-03-31', desc: 'Q1 payment date (ISO-8601)'},
            {name: 'q2_amount', required: false, example: '756.75', desc: 'Q2 levy amount charged'},
            {name: 'q2_paid', required: false, example: '756.75', desc: 'Q2 amount paid'},
            {name: 'q2_date', required: false, example: '2026-06-30', desc: 'Q2 payment date'},
            {name: 'q3_amount', required: false, example: '756.75', desc: 'Q3 levy amount charged'},
            {name: 'q3_paid', required: false, example: '0.00', desc: 'Q3 amount paid'},
            {name: 'q3_date', required: false, example: '', desc: 'Q3 payment date'},
            {name: 'q4_amount', required: false, example: '756.75', desc: 'Q4 levy amount charged'},
            {name: 'q4_paid', required: false, example: '0.00', desc: 'Q4 amount paid'},
            {name: 'q4_date', required: false, example: '', desc: 'Q4 payment date'},
            {name: 'arrears_amount', required: false, example: '0.00', desc: 'Total arrears outstanding'},
            {name: 'notes', required: false, example: '', desc: 'Additional notes for this unit'},
        ],
    },
};

const STEPS = [
    {id: 1, label: 'Select Type'},
    {id: 2, label: 'Download Template'},
    {id: 3, label: 'Upload File'},
    {id: 4, label: 'Preview'},
    {id: 5, label: 'Results'},
];

const FINANCIAL_YEARS = ['2024', '2025', '2026', '2027'];

const ALLOWED_ROLES = ['super_admin', 'strata_manager'];

// ─── Stepper ─────────────────────────────────────────────────────────────────

function Stepper({currentStep}) {
    return (
        <div className="flex items-center justify-between mb-8">
            {STEPS.map((step, idx) => {
                const isCompleted = currentStep > step.id;
                const isActive = currentStep === step.id;
                return (
                    <React.Fragment key={step.id}>
                        <div className="flex flex-col items-center gap-1">
                            <div
                                className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-semibold border-2 transition-colors ${
                                    isCompleted
                                        ? 'bg-emerald-600 border-emerald-600 text-white'
                                        : isActive
                                            ? 'bg-white border-emerald-600 text-emerald-600'
                                            : 'bg-white border-gray-300 text-gray-400'
                                }`}
                            >
                                {isCompleted ? <CheckCircle className="w-5 h-5"/> : step.id}
                            </div>
                            <span
                                className={`text-xs font-medium hidden sm:block ${
                                    isActive ? 'text-emerald-600' : isCompleted ? 'text-emerald-600' : 'text-gray-400'
                                }`}
                            >
                {step.label}
              </span>
                        </div>
                        {idx < STEPS.length - 1 && (
                            <div
                                className={`flex-1 h-0.5 mx-2 transition-colors ${
                                    currentStep > step.id ? 'bg-emerald-500' : 'bg-gray-200'
                                }`}
                            />
                        )}
                    </React.Fragment>
                );
            })}
        </div>
    );
}

// ─── Step 1: Select Type ──────────────────────────────────────────────────────

function StepSelectType({csvType, setCsvType, onNext}) {
    return (
        <div>
            <h2 className="text-lg font-semibold text-gray-800 mb-1">Select Import Type</h2>
            <p className="text-sm text-gray-500 mb-6">Choose the type of financial data you want to import.</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                {Object.entries(CSV_TYPES).map(([key, cfg]) => {
                    const Icon = cfg.icon;
                    const isSelected = csvType === key;
                    return (
                        <button
                            key={key}
                            onClick={() => setCsvType(key)}
                            className={`text-left p-5 rounded-xl border-2 transition-all focus:outline-none focus:ring-2 focus:ring-emerald-500 ${
                                isSelected
                                    ? 'border-emerald-500 bg-emerald-50 shadow-sm'
                                    : 'border-gray-200 bg-white hover:border-emerald-300 hover:bg-emerald-50/40'
                            }`}
                        >
                            <div className="flex items-start gap-3">
                                <div
                                    className={`p-2.5 rounded-lg ${
                                        isSelected ? 'bg-emerald-600 text-white' : 'bg-gray-100 text-gray-500'
                                    }`}
                                >
                                    <Icon className="w-5 h-5"/>
                                </div>
                                <div>
                                    <div className="font-semibold text-gray-800 text-sm">{cfg.label}</div>
                                    <div className="text-xs text-gray-500 mt-1 leading-relaxed">{cfg.description}</div>
                                </div>
                            </div>
                        </button>
                    );
                })}
            </div>
            <div className="flex justify-end mt-6">
                <Button onClick={onNext} disabled={!csvType} className="gap-2">
                    Next <ArrowRight className="w-4 h-4"/>
                </Button>
            </div>
        </div>
    );
}

// ─── Step 2: Download Template ────────────────────────────────────────────────

function StepDownloadTemplate({csvType, onNext, onBack, api}) {
    const cfg = CSV_TYPES[ csvType ];
    const Icon = cfg.icon;
    const [downloading, setDownloading] = useState(false);

    const handleDownload = async () => {
        setDownloading(true);
        try {
            const response = await api.get(`/financial-import/templates/${cfg.templateType}`, {
                responseType: 'blob',
            });
            const url = window.URL.createObjectURL(new Blob([response.data]));
            const link = document.createElement('a');
            link.href = url;
            link.setAttribute('download', cfg.templateName);
            document.body.appendChild(link);
            link.click();
            link.remove();
            window.URL.revokeObjectURL(url);
            toast.success('Template downloaded successfully');
        } catch {
            toast.error('Failed to download template. Please try again.');
        } finally {
            setDownloading(false);
        }
    };

    return (
        <div>
            <div className="flex items-center gap-3 mb-4">
                <div className="p-2.5 rounded-lg bg-emerald-100 text-emerald-700">
                    <Icon className="w-5 h-5"/>
                </div>
                <div>
                    <h2 className="text-lg font-semibold text-gray-800">{cfg.label}</h2>
                    <p className="text-sm text-gray-500">Download the CSV template and fill in your data</p>
                </div>
            </div>

            <Card className="mb-4">
                <CardContent className="pt-5">
                    <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                        <div className="flex items-center gap-3">
                            <FileText className="w-8 h-8 text-emerald-600"/>
                            <div>
                                <div className="font-medium text-gray-800">{cfg.templateName}</div>
                                <div className="text-xs text-gray-500">CSV template with required columns</div>
                            </div>
                        </div>
                        <Button onClick={handleDownload} disabled={downloading} variant="outline"
                                className="gap-2 shrink-0">
                            {downloading ? <Loader2 className="w-4 h-4 animate-spin"/> :
                                <Download className="w-4 h-4"/>}
                            {downloading ? 'Downloading…' : 'Download Template'}
                        </Button>
                    </div>
                </CardContent>
            </Card>

            <Card>
                <CardHeader className="pb-2">
                    <CardTitle className="text-sm font-semibold text-gray-700">Column Reference</CardTitle>
                </CardHeader>
                <CardContent className="p-0">
                    <div className="overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow className="bg-gray-50">
                                    <TableHead className="text-xs">Column</TableHead>
                                    <TableHead className="text-xs">Required</TableHead>
                                    <TableHead className="text-xs">Example</TableHead>
                                    <TableHead className="text-xs">Description</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {cfg.columns.map((col) => (
                                    <TableRow key={col.name}>
                                        <TableCell className="font-mono text-xs text-blue-700">{col.name}</TableCell>
                                        <TableCell>
                                            {col.required ? (
                                                <Badge variant="destructive"
                                                       className="text-xs px-1.5 py-0">Required</Badge>
                                            ) : (
                                                <Badge variant="secondary"
                                                       className="text-xs px-1.5 py-0">Optional</Badge>
                                            )}
                                        </TableCell>
                                        <TableCell
                                            className="font-mono text-xs text-gray-600">{col.example || '—'}</TableCell>
                                        <TableCell className="text-xs text-gray-600">{col.desc}</TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </div>
                </CardContent>
            </Card>

            <div className="flex justify-between mt-6">
                <Button variant="outline" onClick={onBack} className="gap-2">
                    <ArrowLeft className="w-4 h-4"/> Back
                </Button>
                <Button onClick={onNext} className="gap-2">
                    Next <ArrowRight className="w-4 h-4"/>
                </Button>
            </div>
        </div>
    );
}

// ─── Step 3: Upload File ──────────────────────────────────────────────────────

function StepUploadFile({csvType, file, setFile, selectedYear, setSelectedYear, onNext, onBack}) {
    const cfg = CSV_TYPES[ csvType ];
    const fileInputRef = useRef(null);
    const [dragging, setDragging] = useState(false);

    const handleFile = (f) => {
        if (f && f.name.endsWith('.csv')) {
            setFile(f);
        } else {
            toast.error('Please upload a .csv file');
        }
    };

    const onDrop = (e) => {
        e.preventDefault();
        setDragging(false);
        const f = e.dataTransfer.files[ 0 ];
        handleFile(f);
    };

    const formatBytes = (bytes) => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${( bytes / 1024 ).toFixed(1)} KB`;
        return `${( bytes / ( 1024 * 1024 ) ).toFixed(1)} MB`;
    };

    const requiredCols = cfg.columns.filter((c) => c.required).map((c) => c.name);

    return (
        <div>
            <h2 className="text-lg font-semibold text-gray-800 mb-1">Upload CSV File</h2>
            <p className="text-sm text-gray-500 mb-6">Upload your {cfg.label} CSV file for import.</p>

            {/* Year selector */}
            <div className="mb-5">
                <label className="block text-sm font-medium text-gray-700 mb-1.5">Financial Year</label>
                <Select value={selectedYear} onValueChange={setSelectedYear}>
                    <SelectTrigger className="w-48">
                        <SelectValue placeholder="Select year"/>
                    </SelectTrigger>
                    <SelectContent>
                        {FINANCIAL_YEARS.map((y) => (
                            <SelectItem key={y} value={y}>{y}–{String(Number(y) + 1).slice(2)}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
            </div>

            {/* Drop zone */}
            <div
                className={`relative border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
                    dragging ? 'border-emerald-500 bg-emerald-50' : 'border-gray-300 hover:border-emerald-400 hover:bg-emerald-50/30'
                }`}
                onDragOver={(e) => {
                    e.preventDefault();
                    setDragging(true);
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={onDrop}
                onClick={() => fileInputRef.current?.click()}
            >
                <input
                    ref={fileInputRef}
                    type="file"
                    accept=".csv"
                    className="hidden"
                    onChange={(e) => handleFile(e.target.files?.[ 0 ])}
                />
                {file ? (
                    <div className="flex flex-col items-center gap-2">
                        <FileText className="w-10 h-10 text-emerald-600"/>
                        <div className="font-medium text-gray-800">{file.name}</div>
                        <div className="text-sm text-gray-500">{formatBytes(file.size)}</div>
                        <Button
                            variant="ghost"
                            size="sm"
                            className="text-red-500 hover:text-red-700 mt-1"
                            onClick={(e) => {
                                e.stopPropagation();
                                setFile(null);
                            }}
                        >
                            Remove file
                        </Button>
                    </div>
                ) : (
                    <div className="flex flex-col items-center gap-3">
                        <Upload className="w-10 h-10 text-gray-400"/>
                        <div>
                            <div className="font-medium text-gray-700">Drag & drop your CSV file here</div>
                            <div className="text-sm text-gray-500 mt-1">or click to browse</div>
                        </div>
                        <Badge variant="secondary" className="text-xs">.csv files only</Badge>
                    </div>
                )}
            </div>

            {/* Validation reminder */}
            <Alert className="mt-4 bg-blue-50 border-blue-200">
                <AlertCircle className="w-4 h-4 text-blue-600"/>
                <AlertTitle className="text-sm font-semibold text-blue-800">Required columns</AlertTitle>
                <AlertDescription className="text-xs text-blue-700 mt-1">
                    <span className="font-mono">{requiredCols.join(', ')}</span>
                </AlertDescription>
            </Alert>

            <div className="flex justify-between mt-6">
                <Button variant="outline" onClick={onBack} className="gap-2">
                    <ArrowLeft className="w-4 h-4"/> Back
                </Button>
                <Button onClick={onNext} disabled={!file} className="gap-2">
                    Preview Import <ArrowRight className="w-4 h-4"/>
                </Button>
            </div>
        </div>
    );
}

// ─── Step 4: Preview ──────────────────────────────────────────────────────────

function StepPreview({csvType, file, selectedYear, previewData, uploading, onConfirm, onBack}) {
    const cfg = CSV_TYPES[ csvType ];
    const summary = previewData?.summary || {};
    const errors = previewData?.errors || [];
    const warnings = previewData?.warnings || [];
    const previewRows = previewData?.preview_rows || [];
    const previewColumns = previewRows.length > 0 ? Object.keys(previewRows[ 0 ]) : [];

    return (
        <div>
            <h2 className="text-lg font-semibold text-gray-800 mb-1">Preview Import</h2>
            <p className="text-sm text-gray-500 mb-5">
                Review the data before confirming the import for FY {selectedYear}.
            </p>

            {/* Summary cards */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
                {[
                    {label: 'Total Rows', value: summary.total_rows ?? 0, color: 'text-gray-700'},
                    {label: 'Ready', value: summary.ready ?? summary.valid_rows ?? 0, color: 'text-emerald-600'},
                    {label: 'Warnings', value: warnings.length, color: 'text-amber-600'},
                    {label: 'Errors', value: errors.length, color: 'text-red-600'},
                ].map((s) => (
                    <Card key={s.label}>
                        <CardContent className="pt-4 pb-3 text-center">
                            <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
                            <div className="text-xs text-gray-500 mt-0.5">{s.label}</div>
                        </CardContent>
                    </Card>
                ))}
            </div>

            {/* Errors */}
            {errors.length > 0 && (
                <Alert variant="destructive" className="mb-4">
                    <AlertCircle className="w-4 h-4"/>
                    <AlertTitle>Errors found ({errors.length})</AlertTitle>
                    <AlertDescription>
                        <ul className="mt-1 space-y-0.5 text-xs list-disc list-inside">
                            {errors.slice(0, 10).map((e, i) => (
                                <li key={i}>{typeof e === 'string' ? e : e.message || JSON.stringify(e)}</li>
                            ))}
                            {errors.length > 10 && <li>…and {errors.length - 10} more</li>}
                        </ul>
                    </AlertDescription>
                </Alert>
            )}

            {/* Warnings */}
            {warnings.length > 0 && (
                <Alert className="mb-4 bg-amber-50 border-amber-200">
                    <AlertCircle className="w-4 h-4 text-amber-600"/>
                    <AlertTitle className="text-amber-800">Warnings ({warnings.length})</AlertTitle>
                    <AlertDescription>
                        <ul className="mt-1 space-y-0.5 text-xs text-amber-700 list-disc list-inside">
                            {warnings.slice(0, 5).map((w, i) => (
                                <li key={i}>{typeof w === 'string' ? w : w.message || JSON.stringify(w)}</li>
                            ))}
                            {warnings.length > 5 && <li>…and {warnings.length - 5} more</li>}
                        </ul>
                    </AlertDescription>
                </Alert>
            )}

            {/* Sample rows */}
            {previewRows.length > 0 && (
                <Card className="mb-4">
                    <CardHeader className="pb-2">
                        <CardTitle className="text-sm text-gray-700">
                            Sample Data — first {previewRows.length} row{previewRows.length !== 1 ? 's' : ''}
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="p-0">
                        <div className="overflow-x-auto">
                            <Table>
                                <TableHeader>
                                    <TableRow className="bg-gray-50">
                                        {previewColumns.map((col) => (
                                            <TableHead key={col}
                                                       className="text-xs font-mono whitespace-nowrap">{col}</TableHead>
                                        ))}
                                    </TableRow>
                                </TableHeader>
                                <TableBody>
                                    {previewRows.map((row, i) => (
                                        <TableRow key={i}>
                                            {previewColumns.map((col) => (
                                                <TableCell key={col} className="text-xs whitespace-nowrap">
                                                    {String(row[ col ] ?? '—')}
                                                </TableCell>
                                            ))}
                                        </TableRow>
                                    ))}
                                </TableBody>
                            </Table>
                        </div>
                    </CardContent>
                </Card>
            )}

            <div className="flex justify-between mt-6">
                <Button variant="outline" onClick={onBack} disabled={uploading} className="gap-2">
                    <ArrowLeft className="w-4 h-4"/> Start Over
                </Button>
                <Button
                    onClick={onConfirm}
                    disabled={uploading || errors.length > 0}
                    className="gap-2 bg-emerald-600 hover:bg-emerald-700"
                >
                    {uploading ? (
                        <><Loader2 className="w-4 h-4 animate-spin"/> Importing…</>
                    ) : (
                        <><CheckCircle className="w-4 h-4"/> Confirm Import</>
                    )}
                </Button>
            </div>
            {errors.length > 0 && (
                <p className="text-xs text-red-600 text-right mt-2">
                    Fix all errors before confirming import.
                </p>
            )}
        </div>
    );
}

// ─── Step 5: Results ──────────────────────────────────────────────────────────

function StepResults({importResult, onImportAnother, csvType}) {
    const cfg = CSV_TYPES[ csvType ];
    const r = importResult || {};
    const status = r.status || ( r.errors_count > 0 && r.imported_count === 0 ? 'failed' : r.errors_count > 0 ? 'partial' : 'success' );
    const resultErrors = r.errors || r.error_details || [];

    const statusConfig = {
        success: {
            label: 'Import Successful',
            color: 'bg-emerald-100 text-emerald-800 border-emerald-200',
            icon: CheckCircle,
            iconColor: 'text-emerald-600'
        },
        partial: {
            label: 'Partial Import',
            color: 'bg-amber-100 text-amber-800 border-amber-200',
            icon: AlertCircle,
            iconColor: 'text-amber-600'
        },
        failed: {
            label: 'Import Failed',
            color: 'bg-red-100 text-red-800 border-red-200',
            icon: AlertCircle,
            iconColor: 'text-red-600'
        },
    };

    const sc = statusConfig[ status ] || statusConfig.success;
    const StatusIcon = sc.icon;

    return (
        <div>
            <div className="flex flex-col items-center text-center mb-8">
                <StatusIcon className={`w-14 h-14 mb-3 ${sc.iconColor}`}/>
                <h2 className="text-xl font-semibold text-gray-800">{sc.label}</h2>
                <p className="text-sm text-gray-500 mt-1">
                    {cfg.label} import for FY {r.financial_year || '—'} is complete.
                </p>
            </div>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
                {[
                    {label: 'Imported', value: r.imported_count ?? r.imported ?? 0, color: 'text-emerald-600'},
                    {label: 'Updated', value: r.updated_count ?? r.updated ?? 0, color: 'text-blue-600'},
                    {label: 'Skipped', value: r.skipped_count ?? r.skipped ?? 0, color: 'text-gray-500'},
                    {label: 'Errors', value: r.errors_count ?? resultErrors.length, color: 'text-red-600'},
                ].map((s) => (
                    <Card key={s.label}>
                        <CardContent className="pt-4 pb-3 text-center">
                            <div className={`text-2xl font-bold ${s.color}`}>{s.value}</div>
                            <div className="text-xs text-gray-500 mt-0.5">{s.label}</div>
                        </CardContent>
                    </Card>
                ))}
            </div>

            {resultErrors.length > 0 && (
                <Alert variant="destructive" className="mb-6">
                    <AlertCircle className="w-4 h-4"/>
                    <AlertTitle>Error Details</AlertTitle>
                    <AlertDescription>
                        <ul className="mt-1 space-y-0.5 text-xs list-disc list-inside">
                            {resultErrors.slice(0, 15).map((e, i) => (
                                <li key={i}>{typeof e === 'string' ? e : e.message || JSON.stringify(e)}</li>
                            ))}
                            {resultErrors.length > 15 && <li>…and {resultErrors.length - 15} more</li>}
                        </ul>
                    </AlertDescription>
                </Alert>
            )}

            <div className="flex flex-col sm:flex-row gap-3 justify-center">
                <Button onClick={onImportAnother} variant="outline" className="gap-2">
                    <RefreshCw className="w-4 h-4"/> Import Another
                </Button>
                <Button
                    onClick={() => window.location.href = '/dashboard/finance'}
                    className="gap-2 bg-emerald-600 hover:bg-emerald-700"
                >
                    View Finance Dashboard <ChevronRight className="w-4 h-4"/>
                </Button>
            </div>
        </div>
    );
}

// ─── Import History ───────────────────────────────────────────────────────────

function ImportHistory({history, loading, onRefresh}) {
    const statusBadge = (status) => {
        const map = {
            completed: 'bg-emerald-100 text-emerald-800',
            success: 'bg-emerald-100 text-emerald-800',
            partial: 'bg-amber-100 text-amber-800',
            failed: 'bg-red-100 text-red-800',
            pending: 'bg-blue-100 text-blue-800',
        };
        return map[ status ] || 'bg-gray-100 text-gray-700';
    };

    return (
        <Card className="mt-8">
            <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                        <History className="w-4 h-4 text-gray-500"/>
                        <CardTitle className="text-sm font-semibold text-gray-700">Recent Import History</CardTitle>
                    </div>
                    <Button variant="ghost" size="sm" onClick={onRefresh} disabled={loading} className="h-7 gap-1">
                        {loading ? <Loader2 className="w-3.5 h-3.5 animate-spin"/> :
                            <RefreshCw className="w-3.5 h-3.5"/>}
                        <span className="text-xs">Refresh</span>
                    </Button>
                </div>
            </CardHeader>
            <CardContent className="p-0">
                {loading ? (
                    <div className="flex justify-center items-center py-8 text-gray-400">
                        <Loader2 className="w-5 h-5 animate-spin mr-2"/> Loading history…
                    </div>
                ) : history.length === 0 ? (
                    <div className="text-center py-8 text-sm text-gray-400">No import history yet.</div>
                ) : (
                    <div className="overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow className="bg-gray-50">
                                    <TableHead className="text-xs">Type</TableHead>
                                    <TableHead className="text-xs">Year</TableHead>
                                    <TableHead className="text-xs">File</TableHead>
                                    <TableHead className="text-xs text-right">Imported</TableHead>
                                    <TableHead className="text-xs text-right">Errors</TableHead>
                                    <TableHead className="text-xs">Status</TableHead>
                                    <TableHead className="text-xs">Date</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {history.map((h, i) => (
                                    <TableRow key={h.id || i}>
                                        <TableCell className="text-xs font-medium">
                                            {CSV_TYPES[ h.import_type ]?.label || h.import_type || '—'}
                                        </TableCell>
                                        <TableCell className="text-xs">{h.financial_year || '—'}</TableCell>
                                        <TableCell
                                            className="text-xs text-gray-500 truncate max-w-[160px]">{h.filename || '—'}</TableCell>
                                        <TableCell
                                            className="text-xs text-right">{h.imported_count ?? h.imported ?? 0}</TableCell>
                                        <TableCell
                                            className="text-xs text-right text-red-600">{h.errors_count ?? h.errors ?? 0}</TableCell>
                                        <TableCell>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${statusBadge(h.status)}`}>
                        {h.status || 'unknown'}
                      </span>
                                        </TableCell>
                                        <TableCell className="text-xs text-gray-500 whitespace-nowrap">
                                            {h.created_at
                                                ? new Date(h.created_at).toLocaleDateString('en-AU', {
                                                    day: '2-digit',
                                                    month: 'short',
                                                    year: 'numeric'
                                                })
                                                : '—'}
                                        </TableCell>
                                    </TableRow>
                                ))}
                            </TableBody>
                        </Table>
                    </div>
                )}
            </CardContent>
        </Card>
    );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

const FinancialYearImportPage = () => {
    const {user, api} = useAuth();

    const [step, setStep] = useState(1);
    const [csvType, setCsvType] = useState(null);
    const [selectedYear, setSelectedYear] = useState('2026');
    const [file, setFile] = useState(null);
    const [uploading, setUploading] = useState(false);
    const [previewData, setPreviewData] = useState(null);
    const [importResult, setImportResult] = useState(null);
    const [importHistory, setImportHistory] = useState([]);
    const [historyLoading, setHistoryLoading] = useState(false);

    const isAuthorized = user && ALLOWED_ROLES.includes(user.role);

    const fetchHistory = useCallback(async () => {
        setHistoryLoading(true);
        try {
            const res = await api.get('/financial-import/history?limit=10');
            const data = res.data?.imports ?? res.data;
            setImportHistory(Array.isArray(data) ? data : []);
        } catch {
            // History is non-critical — fail silently
        } finally {
            setHistoryLoading(false);
        }
    }, [api]);

    useEffect(() => {
        if (isAuthorized) fetchHistory();
    }, [isAuthorized, fetchHistory]);

    const handleUploadAndPreview = async () => {
        if (!file || !csvType) return;
        setUploading(true);
        try {
            const cfg = CSV_TYPES[ csvType ];
            const formData = new FormData();
            formData.append('file', file);
            formData.append('financial_year', selectedYear);
            formData.append('preview_only', 'true');

            const res = await api.post(`/financial-import/${cfg.endpoint}`, formData, {
                headers: {'Content-Type': 'multipart/form-data'},
            });
            setPreviewData(res.data);
            setStep(4);
        } catch (err) {
            const msg = err?.response?.data?.detail || err?.response?.data?.message || 'Upload failed. Please try again.';
            toast.error(msg);
        } finally {
            setUploading(false);
        }
    };

    const handleConfirmImport = async () => {
        if (!file || !csvType) return;
        setUploading(true);
        try {
            const cfg = CSV_TYPES[ csvType ];
            const formData = new FormData();
            formData.append('file', file);
            formData.append('financial_year', selectedYear);

            const res = await api.post(`/financial-import/${cfg.endpoint}`, formData, {
                headers: {'Content-Type': 'multipart/form-data'},
            });
            setImportResult(res.data);
            setStep(5);
            toast.success('Import completed!');
            fetchHistory();
        } catch (err) {
            const msg = err?.response?.data?.detail || err?.response?.data?.message || 'Import failed. Please try again.';
            toast.error(msg);
        } finally {
            setUploading(false);
        }
    };

    const handleStartOver = () => {
        setStep(1);
        setCsvType(null);
        setFile(null);
        setPreviewData(null);
        setImportResult(null);
    };

    if (!isAuthorized) {
        return (
            <div className="max-w-2xl mx-auto mt-16 px-4">
                <Alert variant="destructive">
                    <AlertCircle className="w-4 h-4"/>
                    <AlertTitle>Access Restricted</AlertTitle>
                    <AlertDescription>
                        Access restricted to Strata Managers, Administrators and Chairman.
                    </AlertDescription>
                </Alert>
            </div>
        );
    }

    return (
        <div className="max-w-4xl mx-auto px-4 py-8">
            {/* Page header */}
            <div className="mb-6">
                <div className="flex items-center gap-3">
                    <div className="p-2.5 rounded-xl bg-emerald-600 text-white">
                        <Upload className="w-5 h-5"/>
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-gray-900">Financial Year Import</h1>
                        <p className="text-sm text-gray-500 mt-0.5">
                            Upload and import comprehensive financial year data via CSV
                        </p>
                    </div>
                </div>
            </div>

            {/* Wizard card */}
            <Card>
                <CardContent className="pt-6">
                    <Stepper currentStep={step}/>

                    {step === 1 && (
                        <StepSelectType
                            csvType={csvType}
                            setCsvType={setCsvType}
                            onNext={() => setStep(2)}
                        />
                    )}

                    {step === 2 && csvType && (
                        <StepDownloadTemplate
                            csvType={csvType}
                            onNext={() => setStep(3)}
                            onBack={() => setStep(1)}
                            api={api}
                        />
                    )}

                    {step === 3 && csvType && (
                        <StepUploadFile
                            csvType={csvType}
                            file={file}
                            setFile={setFile}
                            selectedYear={selectedYear}
                            setSelectedYear={setSelectedYear}
                            onNext={handleUploadAndPreview}
                            onBack={() => setStep(2)}
                        />
                    )}

                    {step === 4 && csvType && (
                        <StepPreview
                            csvType={csvType}
                            file={file}
                            selectedYear={selectedYear}
                            previewData={previewData}
                            uploading={uploading}
                            onConfirm={handleConfirmImport}
                            onBack={handleStartOver}
                        />
                    )}

                    {step === 5 && csvType && (
                        <StepResults
                            importResult={importResult}
                            csvType={csvType}
                            onImportAnother={handleStartOver}
                        />
                    )}
                </CardContent>
            </Card>

            {/* Import history */}
            <ImportHistory
                history={importHistory}
                loading={historyLoading}
                onRefresh={fetchHistory}
            />
        </div>
    );
};

export default FinancialYearImportPage;
