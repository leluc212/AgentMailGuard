"""Offline, aggregate-level audit and monochrome figures for the three saved v3 runs.

Run with Python and matplotlib==3.9.2; no provider calls. Original artifacts are preserved.
"""
from pathlib import Path
import csv
import hashlib
import json
import math
import os
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
os.environ.setdefault('MPLCONFIGDIR', str(ROOT / '.chart-deps' / 'mpl-cache'))
if (ROOT / '.chart-deps').exists():
    sys.path.insert(0, str(ROOT / '.chart-deps'))
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

RUNS = {
    'GPT-4o-mini': '2026-10-19-gpt4omini-v3-1800',
    'Qwen2.5-7B': '2026-10-19-qwen25-7b-openrouter-v3-1800',
    'Llama-3.1-8B': '2026-10-19-llama31-8b-openrouter-v3-1800',
}
CONFIGS = ['C0', 'C0T', 'C1', 'C2', 'C3', 'C3-L1', 'C3-L2', 'C3-L3', 'C3-L4', 'C3-ISO']
MODELS = list(RUNS)
INPUTS = set()
DATA = []
CHECKS = []
TABLE_DIFFERENCES = []


def read_json(path):
    INPUTS.add(path)
    return json.loads(path.read_text(encoding='utf-8'))


def wilson(k, n):
    if n <= 0 or not 0 <= k <= n:
        raise ValueError(f'Invalid binomial counts: {k}/{n}')
    z = 1.96  # Match the pre-registered benchmark's rounded normal quantile.
    p = k / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return max(0, c-h), min(1, c+h)


def identify(label):
    return next(m for m in MODELS if m.lower() in label.lower())


def rate(d):
    return 100 * d['successes'] / d['total']


def validate_rate(d):
    lo, hi = wilson(d['successes'], d['total'])
    assert abs(lo-d['lo']) < 1e-6 and abs(hi-d['hi']) < 1e-6, d


def load():
    summaries, manifests = {}, {}
    for m, run in RUNS.items():
        p = ROOT / 'evaluation/results/mailguard_bench' / run
        summaries[m] = read_json(p / 'summary.json')
        manifests[m] = read_json(p / 'manifest.json')
        read_json(p / 'case_manifest.json')
        INPUTS.add(p / 'metrics.csv')
    return summaries, manifests


def audit(summaries, manifests):
    old_path = ROOT / 'paper/figures/final/3models/figure_data.csv'
    INPUTS.add(old_path)
    old = list(csv.DictReader(old_path.read_text(encoding='utf-8').splitlines()))
    compared = 0
    for r in old:
        k, n = int(r['k']), int(r['n_scored'])
        lo, hi = wilson(k, n)
        assert abs(float(r['pct']) - 100*k/n) <= .0051
        assert abs(float(r['ci95_lo']) - 100*lo) <= .0051
        assert abs(float(r['ci95_hi']) - 100*hi) <= .0051
        if r['figure'] == 'fig4':
            continue
        m = identify(r['group'] if r['figure'] in ('fig1', 'fig5') else r['series'])
        c = r['series'].split(':')[0] if r['figure'] in ('fig1', 'fig5') else r['group']
        table = 'llmail' if r['figure'] == 'fig5' else r['panel']
        key = ('fpr' if r['panel'] == 'FPR' else 'utility') if r['figure'] == 'fig5' else 'asr'
        d = summaries[m]['tables'][table][c][key]
        expected = d['total']-d['successes'] if key == 'asr' else d['successes']
        assert (k, n) == (expected, d['total']), (r, d)
        compared += 1
    CHECKS.append(f'{len(old)}/{len(old)} original plotted percentages and Wilson intervals reproduce from their CSV counts.')
    CHECKS.append(f'{compared}/{compared} non-source-breakdown plotted count pairs match summary.json exactly.')
    import pdfplumber
    table_path = ROOT / 'paper/figures/final/3models/fig6_summary_table.pdf'
    INPUTS.add(table_path)
    with pdfplumber.open(table_path) as original_pdf:
        cells = original_pdf.pages[0].extract_table()
    assert len(cells) == 31
    for row in cells[1:]:
        m, c = identify(row[0]), row[1]
        a = summaries[m]['tables']['llmail'][c]
        b = summaries[m]['tables']['rag'][c]
        for col, d in [(2,a['asr']),(4,b['asr'])]:
            expected = f'{100-rate(d):.1f} [{100*(1-d["hi"]):.1f}, {100*(1-d["lo"]):.1f}]'
            if row[col] != expected:
                TABLE_DIFFERENCES.append(f'{m} {c}, {"email" if col==2 else "RAG"} defense: rendered {row[col]}; directly recomputed {expected}')
        assert row[3] == f'{a["asr"]["total"]}/1000'
        assert row[5] == f'{b["asr"]["total"]}/300'
        assert row[6] == f'{rate(a["fpr"]):.1f}'
        assert row[7] == f'{rate(a["utility"]):.1f}'
        assert row[8] == f'{a["utility"]["total"]}/500'
    CHECKS.append(f'All 30 original PDF summary rows have matching denominators and benign rates; {len(TABLE_DIFFERENCES)} attack-rate/interval display strings differ from direct one-decimal recomputation (details below).')
    # The old source breakdown is not independently re-scoreable without raw records.
    for m in MODELS:
        for c in ['C0', 'C3']:
            rows = [r for r in old if r['figure']=='fig4' and identify(r['group'])==m and r['series'].startswith(c+':')]
            d = summaries[m]['tables']['rag'][c]['asr']
            assert sum(int(r['k']) for r in rows) == d['total']-d['successes']
            assert sum(int(r['n_scored']) for r in rows) == d['total']
    CHECKS.append('All six source-breakdown totals reconcile to saved RAG totals; source assignments remain unverified.')
    mapping = {'ASR':'asr', 'guard_ASR':'guard_asr', 'FPR':'fpr', 'guard_FPR':'guard_fpr',
               'guard_FPR_blocked':'guard_fpr_blocked', 'benign_utility':'utility'}
    csv_checked = 0
    semantic_checked = 0
    for m, summary in summaries.items():
        p = ROOT / 'evaluation/results/mailguard_bench' / RUNS[m]
        for r in csv.DictReader((p/'metrics.csv').read_text(encoding='utf-8').splitlines()):
            if r['table'] in ('llmail','rag') and r['metric'] in mapping and r['group']=='all':
                d = summary['tables'][r['table']][r['config']][mapping[r['metric']]]
                assert (int(r['successes']),int(r['total'])) == (d['successes'],d['total'])
                csv_checked += 1
        for c in CONFIGS:
            for table in ('llmail','rag'):
                a = summary['tables'][table][c]
                for key in ('asr','guard_asr','fpr','utility','guard_fpr','guard_fpr_blocked'):
                    if a[key] is not None and a[key]['total']:
                        validate_rate(a[key])
            path = p/'analysis'/f'meaning__{c}.jsonl'
            INPUTS.add(path)
            latest = {}
            for line in path.read_text(encoding='utf-8').splitlines():
                r = json.loads(line)
                latest[r['case_id']] = r
            for table in ('llmail','rag'):
                rows = [r for r in latest.values() if r['attack']==table and r['status']=='ok']
                decided = [r for r in rows if r['verdict'] in ('succeeded','failed')]
                k = sum(r['verdict']=='succeeded' for r in decided)
                d = summary['tables'][table][c]['meaning']['asr']
                assert (k, len(decided)) == (d['successes'],d['total']), (m,c,table,k,len(decided),d)
                semantic_checked += 1
    CHECKS.append(f'{csv_checked} selected metrics.csv rows agree with summary.json.')
    CHECKS.append(f'{semantic_checked} semantic pipeline rates independently recounted from latest saved meaning verdicts; draft freshness cannot be checked without raw files.')
    for key in ('cases_sha256','rag_email_commit','mailguard_commit','l1_model_sha256','embedding'):
        values = [json.dumps(manifests[m]['runs'][c][key],sort_keys=True) for m in MODELS for c in CONFIGS]
        assert len(set(values))==1, key
    CHECKS.append('All 30 run/config manifests share case hash, host commit, guard commit, L1 classifier hash and embedding settings.')
    return old


def metric(summaries, m, c, table, key):
    d = summaries[m]['tables'][table][c]
    return d['meaning']['asr'] if key=='meaning_asr' else d[key]


def point(ax, x, d, marker='o', filled=True, hatch=''):
    p = rate(d)
    lo, hi = wilson(d['successes'],d['total'])
    ax.bar(x,p,width=.72,color='black' if filled else 'white',edgecolor='black',hatch=hatch,zorder=3)
    ax.errorbar(x,p,yerr=[[max(0,p-100*lo)],[max(0,100*hi-p)]],fmt='none',
                ecolor='black',capsize=3,lw=1.1,zorder=4)
    ax.text(x,100*hi+2,f'{p:.2f}%\n{d["successes"]}/{d["total"]}',
            ha='center',va='bottom',fontsize=8.5,linespacing=1.3)


def column_axes(ax, title):
    ax.set_ylim(0,124)
    ax.set_yticks([0,20,40,60,80,100])
    ax.set_ylabel('Rate (%)')
    ax.set_title(title,loc='left',fontsize=12,pad=24)
    ax.grid(axis='y',color='.85',lw=.6,zorder=0)
    ax.spines[['top','right']].set_visible(False)
    ax.tick_params(axis='x',length=0,labelsize=10)


def panel(ax, summaries, table, key, configs, title, figid):
    paired = len(configs)==2
    for m in MODELS:
        for c in configs:
            d=metric(summaries,m,c,table,key)
            i,j=MODELS.index(m),configs.index(c)
            x=i*2.8+j if paired else j*3.8+i
            point(ax,x,d,filled=(j==1 if paired else i==2),hatch='///' if not paired and i==1 else '')
            lo,hi=wilson(d['successes'],d['total'])
            DATA.append(dict(figure=figid,model=m,config=c,population=table,metric=key,
                             k=d['successes'],n=d['total'],pct=rate(d),ci95_lo=100*lo,ci95_hi=100*hi,
                             evidence='saved aggregate; semantic pipeline independently recounted' if key=='meaning_asr' else 'saved aggregate'))
    if paired:
        ax.set_xticks([i*2.8+.5 for i in range(3)],MODELS)
        handles=[Patch(facecolor='white',edgecolor='black',label=configs[0]),Patch(facecolor='black',edgecolor='black',label=configs[1])]
    else:
        ax.set_xticks([j*3.8+1 for j in range(len(configs))],configs)
        handles=[Patch(facecolor='white',edgecolor='black',label=MODELS[0]),Patch(facecolor='white',edgecolor='black',hatch='///',label=MODELS[1]),Patch(facecolor='black',edgecolor='black',label=MODELS[2])]
    column_axes(ax,title)
    ax.legend(handles=handles,loc='upper right',frameon=False,ncol=len(handles),bbox_to_anchor=(1,1.04),fontsize=9)


def newfig(rows=1,height=7):
    fig,axs=plt.subplots(rows,2,figsize=(17,height),squeeze=False)
    fig.subplots_adjust(left=.07,right=.97,bottom=.18 if height<=8 else .12,top=.84,wspace=.2,hspace=.7)
    return fig,axs


def save(fig,name,title,note,pdf):
    fig.suptitle(title,x=.04,ha='left',fontsize=18,y=.975)
    fig.text(.04,.035,note,fontsize=9,va='bottom',linespacing=1.5)
    fig.savefig(OUT/f'{name}.png',dpi=220,facecolor='white')
    fig.savefig(OUT/f'{name}.pdf',facecolor='white')
    fig.savefig(OUT/f'{name}.svg',facecolor='white')
    svg = OUT/f'{name}.svg'
    svg.write_text('\n'.join(line.rstrip() for line in svg.read_text(encoding='utf-8').splitlines())+'\n',encoding='utf-8')
    pdf.savefig(fig,facecolor='white')
    plt.close(fig)


def build(summaries,old):
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10,'pdf.fonttype':42,'svg.fonttype':'none'})
    common='Columns: observed rates; whiskers: nominal 95% Wilson intervals. Labels: rate and events/eligible cases. Lower ASR is better.\nModels serve all LLM roles. Routes: GPT - OpenAI; Qwen - OpenRouter/Phala; Llama - OpenRouter/CoreWeave (bf16). No causal model-ranking claim.'
    with PdfPages(OUT/'all_figures.pdf') as pdf:
        fig,axs=newfig(2,11)
        for i,t in enumerate(['llmail','rag']):
            for j,key in enumerate(['asr','guard_asr']):
                panel(axs[i,j],summaries,t,key,['C0','C3'],f'{t.upper()} | '+('All scored attacks' if j==0 else 'Attacks reaching drafting'),'fig1')
        save(fig,'fig1_guard_effect','01  Attack success: pipeline versus drafting-stage exposure',common,pdf)
        for name,configs,title in [('fig2_configurations',CONFIGS[:5],'02  Configuration comparison: drafting-stage attack success'),
                                   ('fig3_ablation',['C3']+CONFIGS[5:],'03  Remove-one ablation: drafting-stage attack success')]:
            fig,axs=plt.subplots(2,1,figsize=(17,12))
            fig.subplots_adjust(left=.07,right=.97,bottom=.14,top=.87,hspace=.55)
            for j,t in enumerate(['llmail','rag']):
                panel(axs[j],summaries,t,'guard_asr',configs,t.upper()+' | attacks reaching drafting',name)
            note=common+'\nC0T is a template control. L1 inbound; L2 documents; L3 inbound gate; L4 output; ISO channel isolation (not a layer).'
            save(fig,name,title,note,pdf)
        # Retain the source evidence, explicitly provisional rather than pretending raw verification.
        fig,axs=plt.subplots(1,3,figsize=(20,7))
        fig.subplots_adjust(left=.06,right=.98,bottom=.2,top=.79,wspace=.22)
        for ax,source in zip(axs,['poisonedrag','seed_rag','synthetic_poison_v1']):
            for i,m in enumerate(MODELS):
                for j,c in enumerate(['C0','C3']):
                    r=next(r for r in old if r['figure']=='fig4' and r['panel']==source and identify(r['group'])==m and r['series'].startswith(c+':'))
                    k,n=int(r['n_scored'])-int(r['k']),int(r['n_scored'])
                    d={'successes':k,'total':n}
                    x=i*2.8+j; point(ax,x,d,filled=c=='C3')
                    lo,hi=wilson(k,n)
                    DATA.append(dict(figure='fig4',model=m,config=c,population=source,metric='asr',k=k,n=n,pct=100*k/n,ci95_lo=100*lo,ci95_hi=100*hi,evidence='legacy figure CSV; source assignments unverified'))
            ax.set_xticks([i*2.8+.5 for i in range(3)],MODELS)
            column_axes(ax,source.replace('_',' '))
            ax.set_ylabel('Pipeline attack success (%)')
            ax.legend(handles=[Patch(facecolor='white',edgecolor='black',label='C0'),Patch(facecolor='black',label='C3')],loc='upper right',frameon=False,ncol=2,bbox_to_anchor=(1,1.04))
        save(fig,'fig4_sources_provisional','04  RAG source breakdown - PROVISIONAL',
             'Recomputed from the old figure CSV only; source totals reconcile with saved RAG aggregates. Raw source assignments cannot be verified.\nAll scored attacks; string-rule scorer. Synthetic cases remain separate. Do not treat this figure as independently validated.',pdf)
        fig,axs=newfig(2,11)
        for ax,key,title in zip(axs.flat,['fpr','guard_fpr_blocked','guard_fpr','utility'],[
            'Blocked / all scored benign\nLower is better',
            'Blocked / benign reaching drafting\nLower is better',
            'Escalated / benign reaching drafting\nLower is better',
            'Draft-length proxy / all benign\nHigher is better']):
            panel(ax,summaries,'llmail',key,['C0','C3'],title,'fig5')
        save(fig,'fig5_benign_tradeoff','05  Benign trade-offs: four different denominators and outcomes',
             'Blocked includes quarantine; escalated also includes human review with draft kept. Draft-length proxy uses min_draft_chars.v1 (at least 40 characters).\nThis proxy does not establish correctness, relevance or human acceptance. The report flags LLMail benign overlap with L1 training negatives.',pdf)
        fig,ax=plt.subplots(figsize=(17,13)); ax.axis('off')
        cols=['Model / config','Email ASR\nall scored','Email ASR\nreached drafting','RAG ASR\nall scored','RAG ASR\nreached drafting','Benign escalation\nreached drafting','Draft proxy\nall benign','Errors /\n1800 planned']
        rows=[]
        for m in MODELS:
            for c in CONFIGS:
                a=summaries[m]['tables']['llmail'][c]; b=summaries[m]['tables']['rag'][c]
                ds=[a['asr'],a['guard_asr'],b['asr'],b['guard_asr'],a['guard_fpr'],a['utility']]
                rows.append([f'{m} / {c}']+[f'{rate(d):.2f}% ({d["successes"]}/{d["total"]})' for d in ds]+[str(MANIFESTS[m]['counts'][c]['errors'])])
                for d,(population,key) in zip(ds,[('llmail','asr'),('llmail','guard_asr'),('rag','asr'),('rag','guard_asr'),('benign','guard_fpr'),('benign','utility')]):
                    lo,hi=wilson(d['successes'],d['total'])
                    DATA.append(dict(figure='fig6',model=m,config=c,population=population,metric=key,k=d['successes'],n=d['total'],pct=rate(d),ci95_lo=100*lo,ci95_hi=100*hi,evidence='saved aggregate'))
        tbl=ax.table(cellText=rows,colLabels=cols,cellLoc='center',bbox=[0,.09,1,.85],colWidths=[.17]+[.125]*6+[.08])
        tbl.auto_set_font_size(False); tbl.set_fontsize(8)
        for (r,c),cell in tbl.get_celld().items():
            cell.set_linewidth(.35)
            if r==0 or (r>0 and rows[r-1][0].endswith(' / C3')):
                cell.get_text().set_weight('bold')
        save(fig,'fig6_summary','06  Complete three-model scorecard',
             'Every percentage displays its numerator and actual denominator. Errors are excluded from rates, not counted as successful defense.\nCounts come from saved summaries and manifests; full raw re-scoring is unavailable. See audit.md for uncertainty, exclusions and study limitations.',pdf)
        fig,axs=newfig(2,11)
        for i,t in enumerate(['llmail','rag']):
            for j,key in enumerate(['asr','meaning_asr']):
                panel(axs[i,j],summaries,t,key,['C0','C3'],t.upper()+' | '+('String-rule ASR' if j==0 else 'Semantic-reader ASR'),'supp1')
        save(fig,'supp1_scoring_sensitivity','S1  Scoring sensitivity: string rule versus semantic reader',
             'All scored attacks for the string rule; semantic denominator excludes unclear verdicts. Reader: gpt-4.1-mini, meaning-rubric.v1.\nSemantic pipeline counts were independently recounted from saved verdicts. No human calibration is established here; neither scorer is ground truth.',pdf)
        fig,axs=newfig(1,8)
        for j,t in enumerate(['rag','rag_poison_retrieved']):
            panel(axs[0,j],summaries,t,'asr',['C0','C3'], 'All scored RAG attacks' if j==0 else 'RAG attacks with poison retrieved','supp2')
        save(fig,'supp2_retrieval_exposure','S2  Retrieval exposure changes the RAG denominator',
             'Pipeline ASR includes cases where poison was not retrieved. The exposed subset conditions on poisoned material being retrieved.\nThe subsets can differ across configurations and models; these are descriptive rates, not a matched-case causal estimate.',pdf)
    with (OUT/'figure_data.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=DATA[0].keys()); w.writeheader(); w.writerows(DATA)


def report(summaries):
    lines=['# Three-model chart audit - 26 October 2026','',
        '## Conclusion','',
        'The original CSV percentages reproduce from their stated counts. The rendered summary table has minor interval-display discrepancies listed below. The principal corrections concern metric meaning, exposure denominators, omitted semantic scores, and overly broad interpretations. No experimental outcomes have been changed.', '',
        'This is an aggregate-level audit of the 19 October v3 runs. Raw pipeline JSONL records, run-local cases.jsonl, and the original .agents/bench/make_figures_paper.py are absent. Full event-level re-scoring, freshness validation of semantic verdicts, and independent source attribution are therefore incomplete. The new source-breakdown figure is explicitly PROVISIONAL.', '',
        '## Verification','']+['- '+x for x in CHECKS]+['',
        '## Corrections across the six original figures','',
        '| Figure | Finding | Replacement |','|---|---|---|',
        '| 1 | 100 minus pipeline ASR credits triage as well as the guard. | Show pipeline ASR and drafting-stage ASR side by side; lower is better. |',
        '| 2 | A generic defense-rate axis hides the conditional population; C0T is not an undefended prompt. | Explicit drafting-stage denominator; retain C0T as a template control. |',
        '| 3 | Near-100% defense compresses failures and does not establish each layer\'s independent effect. | Plot attack-success rates with counts and intervals; avoid causal layer rankings. |',
        '| 4 | Source totals match aggregate RAG counts, but source-specific raw records are absent. | Preserve source separation, convert to ASR, prominently mark provisional. |',
        '| 5 | Generic FPR uses all benign messages; utility is a minimum-length proxy, not measured quality. | Separate pipeline blocking, exposed blocking, exposed escalation and draft proxy. |',
        '| 6 | A summary without the above distinctions encourages incompatible comparisons. | Regenerate all 30 model/config rows with explicit event/eligible counts and error counts. |','',
        'S1 adds the pre-registered semantic-reader comparison. S2 separates RAG attacks where poisoned material was actually retrieved.', '',
        '## Numerical examples for the defense','',
        '| Model | C3 email guard ASR | C3 RAG guard ASR | C3 benign escalation | C0 to C3 draft proxy |',
        '|---|---|---|---|---|']
    fmt=lambda d:f'{rate(d):.2f}% ({d["successes"]}/{d["total"]})'
    for m in MODELS:
        a=summaries[m]['tables']['llmail']; b=summaries[m]['tables']['rag']
        lines.append(f'| {m} | {fmt(a["C3"]["guard_asr"])} | {fmt(b["C3"]["guard_asr"])} | {fmt(a["C3"]["guard_fpr"])} | {fmt(a["C0"]["utility"])} to {fmt(a["C3"]["utility"])} |')
    lines += ['', '## Original summary-table display discrepancies', ''] + TABLE_DIFFERENCES
    lines += ['', 'These strings are checked against full-precision saved counts/intervals, not rounded metrics.csv values. The replacement calculates directly from counts.', '', '## Missing-case sensitivity (not a confidence interval)', '',
        'For pipeline ASR with k observed successes, n scored cases and N planned cases, the all-planned range is [k/N, (k+N-n)/N]. This treats unknown outcomes as all failures or all successes; neither bound is an observed rate. It must not be substituted for the guard denominator because missing cases have unknown drafting exposure.', '',
        '| Model | C3 email scored/planned | Observed pipeline ASR | All-planned sensitivity range |', '|---|---|---|---|']
    for m in MODELS:
        d=summaries[m]['tables']['llmail']['C3']['asr']; k,n=d['successes'],d['total']
        lines.append(f'| {m} | {n}/1000 | {rate(d):.2f}% | {100*k/1000:.2f}% to {100*(k+1000-n)/1000:.2f}% |')
    lines += ['', '## Interpretation and limitations','',
        '- Zero observed successful attacks is not proof of zero future risk. Whiskers use Wilson 95% intervals and remain nonzero at zero events.',
        '- Intervals are nominal case-level binomial intervals. Attack families, reused templates and related cases may violate independence; these are not cluster-adjusted or repeated-run uncertainty estimates.',
        '- All three models occupy triage, generation and guard-judge roles. Exposure counts change, so comparisons concern whole model/route configurations, not only the reply model.',
        '- The string rule can count an attacker address mentioned in a refusal; the semantic scorer estimates intended behavior and excludes unclear cases. Report both without presenting either as human-validated ground truth.',
        '- The LLMail benchmark is adapted to final drafts. The original Microsoft task uses a specified tool/API call; these results are not directly the original challenge ASR.',
        '- The reports disclose overlap between LLMail benign examples and L1 training negatives. The full benign rate may be optimistic; the Enron-only rate cannot be independently reconstructed from the available raw artifacts.',
        '- The L1 classifier was retrained; report its manifest hash. The attacks were not adaptively optimized against this guard. Synthetic RAG attacks remain separate.',
        '- No new model calls were made, no attack/detection logic was changed, and no operational stack or CI result is claimed.', '',
        '## Suggested answers to lecturers','',
        '**Why did the old graph show a stronger defense?** It plotted the complement of pipeline ASR, including attacks stopped before the guard. The replacement shows pipeline and drafting-stage populations separately.', '',
        '**Is the system 100% secure?** No. We observed zero successful email attacks in the scored C3 sample under this scorer; RAG still has successes, some runs have missing cases, and the confidence interval is not zero-width.', '',
        '**Does a 40-character draft prove usefulness?** No. It is a pre-registered output proxy. Human correctness and acceptance evaluation would require separate evidence.', '',
        '**Which model is best?** These runs show a security/benign-output trade-off under distinct model serving routes and triage behavior. They do not establish a universal winner.', '',
        '## Research sources','',
        '- Microsoft LLMail-Inject analysis and original task definition: https://github.com/microsoft/llmail-inject-challenge-analysis',
        '- NIST guidance on binomial proportions and boundary uncertainty: https://www.itl.nist.gov/div898/software/dataplot/refman2/auxillar/exacbici.htm',
        '- Wilson implementation reference: https://support.sas.com/documentation/cdl/en/procstat/68142/HTML/default/procstat_freq_details37.htm',
        '- Project contracts: ADR-0010, ADR-0011, ADR-0012, ADR-0015; specs/tasks.md 7.19/7.20; R22.12. Saved run reports supply the study-specific caveats.', '',
        '## Reproduce','',
        'Install matplotlib==3.9.2 and pdfplumber in a Python environment, then run `python paper/figures/audit-2026-10-26/build.py` from the repository. The script also recognizes a workspace-local `.chart-deps` installation. All reads are offline. Original figure folders are preserved. Run test_audit.py for the five offline regression checks.', '',
        'See figure_data.csv for every new plotted count and audit_manifest.json for SHA-256 hashes of all inputs. The summary table reads the same saved aggregates. PNG, vector PDF and editable SVG versions are provided.']
    (OUT/'audit.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    manifest={'audit_date':'2026-10-26','level':'aggregate verified; raw pipeline re-scoring incomplete','checks':CHECKS,
              'inputs':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(INPUTS)},
              'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'matplotlib_version':matplotlib.__version__}
    (OUT/'audit_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    SUMMARIES,MANIFESTS=load()
    OLD=audit(SUMMARIES,MANIFESTS)
    build(SUMMARIES,OLD)
    report(SUMMARIES)
    print('\n'.join(CHECKS))
    print(f'Wrote 8 figures and audit to {OUT}')
