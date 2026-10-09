#!/usr/bin/env python3
"""Build a standalone patched script WITHOUT importing or running the original.

Usage:
    python apply_apex_fix.py apex_original.py -o apex_30m_fixed.py

The original is never overwritten or copied into the patch bundle. Credentials
in known environment-default assignments are stripped from the NEW output.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import tempfile
import textwrap

PATCH_VERSION = '2026-10-07 / 30m / CQMI 50-50 / Binance Algo API'
ROOT = Path(__file__).resolve().parent
SECRET_ENV_NAMES = {
    'BINANCE_API_KEY', 'BINANCE_SECRET', 'BYBIT_API_KEY', 'BYBIT_SECRET',
    'OKX_API_KEY', 'OKX_SECRET', 'OKX_PASSPHRASE', 'TELEGRAM_BOT_TOKEN',
    'DISCORD_WEBHOOK_URL', 'SMTP_PASSWORD',
}


class PatchError(ValueError):
    pass


def node_span(node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, 'decorator_list', [])])
    return start - 1, node.end_lineno


def classes_in(tree):
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def methods_in(cls):
    return {node.name: node for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def method_template(file_name, class_name, method_name):
    text = (ROOT / file_name).read_text(encoding='utf-8')
    cls = classes_in(ast.parse(text))[class_name]
    method = methods_in(cls)[method_name]
    start, end = node_span(method)
    return '\n'.join(text.splitlines()[start:end]) + '\n'


def function_template(file_name, name):
    text = (ROOT / file_name).read_text(encoding='utf-8')
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    start, end = node_span(node)
    return '\n'.join(text.splitlines()[start:end]) + '\n'


def embedded_core():
    text = (ROOT / 'core.py').read_text(encoding='utf-8')
    tree = ast.parse(text)
    start, end = node_span(tree.body[0])  # Module docstring, not needed inline.
    lines = text.splitlines()
    del lines[start:end]
    text = '\n'.join(lines) + '\n'
    # Future imports cannot occur in the middle of the original script.
    text = text.replace('from __future__ import annotations\n', '')
    return '\n# === APEX 30M PATCH CORE: ' + PATCH_VERSION + ' ===\n' + text


class AlphaTransform(ast.NodeTransformer):
    def __init__(self):
        self.aqii_calls = 0
        self.legacy_weight_reads = 0

    def visit_Attribute(self, node):
        self.generic_visit(node)
        # Preserve all original directional formulas while gating the weights
        # they actually use. Match direct factor reads and generator `f.weight`.
        if (node.attr == 'weight' and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name)
                and (node.value.id in ('f', 'factor')
                     or node.value.id.endswith('_factor'))):
            self.legacy_weight_reads += 1
            return ast.copy_location(ast.Call(
                func=ast.Attribute(value=ast.Name(id='self', ctx=ast.Load()),
                                   attr='_legacy_factor_weight', ctx=ast.Load()),
                args=[node.value], keywords=[]), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        if (isinstance(node.func, ast.Attribute) and node.func.attr == 'calculate'
                and isinstance(node.func.value, ast.Name) and node.func.value.id == 'aqii_factor'):
            names = ('closes', 'highs', 'lows', 'volumes', 'microstructure')
            node.args = []
            node.keywords = [ast.keyword(arg=n, value=ast.Name(id=n, ctx=ast.Load())) for n in names]
            node.keywords.append(ast.keyword(arg='config', value=ast.Attribute(
                value=ast.Name(id='self', ctx=ast.Load()), attr='config', ctx=ast.Load())))
            self.aqii_calls += 1
        return node

    def visit_Return(self, node):
        self.generic_visit(node)
        if isinstance(node.value, ast.Dict):
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.value.keys]
            if 'action' in keys and 'timeframe' not in keys:
                node.value.keys.append(ast.Constant(value='timeframe'))
                node.value.values.append(ast.Attribute(value=ast.Name(id='CFG', ctx=ast.Load()),
                                                       attr='trading_timeframe', ctx=ast.Load()))
        return node


class SourcePatcher:
    def __init__(self, source):
        if source.lstrip().startswith('```'):
            stripped = source.strip().splitlines()
            if stripped[-1].strip() != '```':
                raise PatchError('Unterminated pasted code fence')
            source = '\n'.join(stripped[1:-1]) + '\n'
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.tree = ast.parse(source)
        self.classes = classes_in(self.tree)
        self.edits = []
        self.changes = []
        self.stripped_literals = []
        required = ('ApexConfig', 'AlphaEngine', 'TrendIndicators', 'VolatilityFactor',
                    'DatabaseSchema', 'BinanceAdapter', 'ApexController', 'CircuitBreaker',
                    'ExecutionEngine', 'OrderManager', 'Signal', 'TradeRepository', 'SignalRepository')
        missing = [name for name in required if name not in self.classes]
        if missing:
            raise PatchError('Source does not match the supplied engine; missing: ' + ', '.join(missing))
        if any(isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'APEX_30M_PATCH_VERSION'
                                                for t in n.targets) for n in self.tree.body):
            raise PatchError('This file is already patched; use the unmodified original')

    def replace(self, node, text, description):
        start, end = node_span(node)
        self.edits.append((start, end, text.rstrip('\n') + '\n'))
        self.changes.append(description)

    def insert(self, line, text, description):
        self.edits.append((line, line, text.rstrip('\n') + '\n'))
        self.changes.append(description)

    def field(self, cls_name, field_name):
        for node in self.classes[cls_name].body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == field_name:
                return node
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == field_name for t in node.targets):
                return node
        raise PatchError(f'Missing expected field: {cls_name}.{field_name}')

    def method(self, cls_name, method_name):
        try:
            return methods_in(self.classes[cls_name])[method_name]
        except KeyError as exc:
            raise PatchError(f'Missing expected method: {cls_name}.{method_name}') from exc

    def original_text(self, node):
        start, end = node_span(node)
        return ''.join(self.lines[start:end])

    def use_template(self, target_class, template_class, method, file_name='other_methods.py'):
        node = self.method(target_class, method)
        self.replace(node, method_template(file_name, template_class, method), target_class + '.' + method)

    def make_edits(self):
        for node in self.tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute)
                            and isinstance(target.value.value, ast.Name) and target.value.value.id == 'os'
                            and target.value.attr == 'environ' and isinstance(target.slice, ast.Constant)
                            and target.slice.value in SECRET_ENV_NAMES):
                        key = target.slice.value
                        if isinstance(node.value, ast.Call):
                            for value in node.value.args[1:]:
                                if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                                    self.stripped_literals.append(value.value)
                        self.replace(node, f'os.environ[{key!r}] = os.getenv({key!r}, "")', 'Strip hard-coded default: ' + key)
                    elif isinstance(target, ast.Name) and target.id == 'BASE_DIR':
                        self.replace(node, 'BASE_DIR = os.path.abspath(os.getenv("APEX_BASE_DIR", os.path.dirname(__file__)))', 'Portable BASE_DIR')
            if isinstance(node, ast.If):
                # Remove ONLY the original import-time credential-exit check.
                constants = {n.value for n in ast.walk(node.test) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
                if {'BINANCE_API_KEY', 'BINANCE_SECRET'} <= constants:
                    self.replace(node, '# Credentials are validated at main(), after DRY_RUN is known.', 'Remove import-time credentials exit')
        self.replace(self.field('ApexConfig', 'dry_run'),
                     '    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"))',
                     'DRY_RUN defaults to true')
        self.replace(self.field('ApexConfig', 'trading_mode'),
                     '    trading_mode: TradingMode = field(default_factory=lambda: TradingMode.DRY_RUN if os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes") else TradingMode.LIVE_TRADING)',
                     'Consistent trading mode')
        self.insert(self.field('ApexConfig', 'primary_exchange').end_lineno,
                    '    trading_timeframe: str = "30m"', 'Single 30m timeframe setting')
        self.replace(self.field('ApexConfig', 'db_path'),
                     '    db_path: str = field(default_factory=lambda: os.getenv("APEX_DB_PATH", "apex_quant_demo.db" if os.getenv("APEX_USE_DEMO", "false").lower() in ("true", "1", "yes") else ("apex_quant_dry_run.db" if os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes") else "apex_quant_v4.db")))',
                     'Separate dry/demo/live databases; preserve original live default')
        self.replace(self.field('AlphaEngine', 'CQMI_WEIGHT'), '    CQMI_WEIGHT = 0.50', 'CQMI weight = 50%')
        self.replace(self.field('AlphaEngine', 'LEGACY_WEIGHT'), '    LEGACY_WEIGHT = 0.50', 'Other directional factors = 50%')
        self.replace(self.field('DatabaseSchema', 'SCHEMA_VERSION'), '    SCHEMA_VERSION = 6', 'Protection schema version = 6')
        self.replace(self.field('Signal', 'timeframe'), '    timeframe: str = "30m"', 'Signal timeframe = 30m')
        for target, template, method in (
            ('TrendIndicators', 'TrendIndicatorsPatch', 'atr'),
            ('VolatilityFactor', 'VolatilityFactorPatch', 'calculate'),
            ('AlphaEngine', 'AlphaEnginePatch', '_compute_legacy_max_score'),
            ('DatabaseSchema', 'DatabaseSchemaPatch', 'apply_schema'),
            ('DatabaseSchema', 'DatabaseSchemaPatch', 'run_migrations'),
            ('BinanceAdapter', 'BinanceAdapterPatch', '__init__'),
            ('BinanceAdapter', 'BinanceAdapterPatch', 'get_ws_streams'),
            ('CircuitBreaker', 'CircuitBreakerPatch', 'update_equity'),
        ):
            self.use_template(target, template, method)
        if '_legacy_factor_weight' in methods_in(self.classes['AlphaEngine']):
            raise PatchError('Unexpected existing legacy-weight helper; use the original source')
        self.insert(self.classes['AlphaEngine'].end_lineno,
                    '\n' + method_template('other_methods.py', 'AlphaEnginePatch', '_legacy_factor_weight'),
                    'One enabled-factor policy for numerator and denominator')
        alpha = self.method('AlphaEngine', 'generate_signal')
        # A detached AST, not execution/import of the user script.
        detached = ast.parse(textwrap.dedent(self.original_text(alpha))).body[0]
        transform = AlphaTransform()
        detached = transform.visit(detached)
        if transform.aqii_calls != 1:
            raise PatchError('Expected exactly one AQII call; source differs from supplied code')
        if transform.legacy_weight_reads == 0:
            raise PatchError('No legacy factor weight references recognized; source differs from supplied code')
        ast.fix_missing_locations(detached)
        alpha_text = textwrap.indent(ast.unparse(detached) + '\n', '    ')
        # Correct the off-by-one: 50 closing prices give only 49 returns.
        alpha_text = alpha_text.replace('QuantMath.simple_returns(closes[-50:])', 'QuantMath.simple_returns(closes[-60:])')
        self.replace(alpha, alpha_text, 'AQII keywords, enabled directional weights, signal timeframe, regime return window')
        core = embedded_core()
        execution = (ROOT / 'execution_replacement.py').read_text(encoding='utf-8')
        self.replace(self.classes['ExecutionEngine'], core + '\n' + execution, 'Actual Algo protection, entry journal, verified failure handling')
        controller_templates = classes_in(ast.parse((ROOT / 'controller_methods.py').read_text(encoding='utf-8')))['ApexControllerPatch']
        for name in methods_in(controller_templates):
            content = method_template('controller_methods.py', 'ApexControllerPatch', name)
            if name in methods_in(self.classes['ApexController']):
                self.replace(self.method('ApexController', name), content, 'Controller: ' + name)
            else:
                self.insert(self.classes['ApexController'].end_lineno, '\n' + content, 'Add controller helper: ' + name)
        verifier = next((node for node in self.tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == '_verify_credentials_loaded'), None)
        if verifier is None:
            raise PatchError('Original credential-verification function missing')
        self.replace(verifier, function_template('other_methods.py', '_verify_credentials_loaded'), 'Runtime validation and explicit order opt-in')
        manager = self.method('OrderManager', 'update_order')
        content = self.original_text(manager)
        old = '(OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)'
        if old not in content:
            raise PatchError('Unexpected OrderManager terminal-status logic')
        self.replace(manager, content.replace(old, '(OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.ERROR, OrderStatus.EXPIRED)'), 'Remove ERROR/EXPIRED orders from active state')
        for cls, name, old, new in (
            ('TradeRepository', 'save_trade', "trade.get('timeframe', '1m')", "trade.get('timeframe', CFG.trading_timeframe)"),
            ('SignalRepository', 'save_signal', "signal.get('timeframe', '1m')", "signal.get('timeframe', CFG.trading_timeframe)"),
        ):
            node = self.method(cls, name)
            content = self.original_text(node)
            if old not in content:
                raise PatchError('Unexpected timeframe default in ' + cls)
            self.replace(node, content.replace(old, new), 'Repository default: ' + cls)
        # Keep optional exchange helpers' defaults consistent without changing
        # Constants.TIMEFRAMES or enum definitions such as TimeFrame.M1.
        replaced_methods = {(start, end) for start, end, _ in self.edits if end > start}
        for cls_name in ('BinanceAdapter', 'BybitAdapter', 'OKXAdapter', 'ExchangeManager'):
            cls = self.classes.get(cls_name)
            if cls is None:
                continue
            method = methods_in(cls).get('fetch_ohlcv')
            if method is not None and node_span(method) not in replaced_methods:
                content = self.original_text(method)
                content = content.replace("timeframe: str = '1m'", "timeframe: str = '30m'")
                content = content.replace('timeframe: str = "1m"', 'timeframe: str = "30m"')
                self.replace(method, content, '30m OHLCV helper default: ' + cls_name)
        # Add a machine-readable marker before the first class, not before the
        # from __future__ statement/shebang/module docstring.
        first_class = min(cls.lineno for cls in self.classes.values())
        self.insert(first_class - 1, 'APEX_30M_PATCH_VERSION = ' + repr(PATCH_VERSION), 'Patch version marker')

    def render(self):
        self.make_edits()
        covered = []
        for start, end, _ in self.edits:
            if start == end:
                continue
            if any(start < old_end and end > old_start for old_start, old_end in covered):
                raise PatchError('Overlapping source edits; no output written')
            covered.append((start, end))
        lines = list(self.lines)
        # Stable order is important for several helper-method inserts at the
        # same class end. These inserts remain indented inside that class.
        for start, end, text in sorted(self.edits, key=lambda item: (item[0], item[1]), reverse=True):
            lines[start:end] = [text]
        result = ''.join(lines)
        for old, new in (
            ('CQMI_WEIGHT = 0.55', 'CQMI_WEIGHT = 0.50'),
            ('Weight: 55% of the final AlphaEngine composite score.', 'Weight: 50% of the final AlphaEngine composite score.'),
            ('CQMI contributes up to 55', 'CQMI contributes up to 50'),
            ('points (55% weight)', 'points (50% weight)'),
            ('contributes up to 45', 'contributes up to 50'),
            ('points (45% weight)', 'points (50% weight)'),
            ('combined later at 55%', 'combined later at 50%'),
            ('CQMI at 55%', 'CQMI at 50%'),
            ('(55% weight)', '(50% weight)'),
            ('QUANTUM APEX v3.0', 'QUANTUM APEX v4.0 (30m patch)'),
            ('Status: PRODUCTION-READY INSTITUTIONAL QUANT ENGINE', 'Status: PATCHED; OFFLINE TESTED CHANGES; NOT PRODUCTION CERTIFIED'),
        ):
            result = result.replace(old, new)
        for literal in self.stripped_literals:
            if literal and literal in result:
                raise PatchError('A stripped credential literal still occurs elsewhere; remove it manually first')
        # Compilation does NOT import or run the resulting code.
        compile(result, '<apex_30m_fixed>', 'exec')
        output_tree = ast.parse(result)
        output_classes = classes_in(output_tree)
        engine = output_classes['AlphaEngine']
        weights = {t.id: n.value.value for n in engine.body if isinstance(n, ast.Assign)
                   and isinstance(n.value, ast.Constant) for t in n.targets if isinstance(t, ast.Name)}
        if weights.get('CQMI_WEIGHT') != .5 or weights.get('LEGACY_WEIGHT') != .5:
            raise PatchError('Final weight validation failed')
        for cls_name in ('ExecutionEngine', 'ApexController'):
            text = ast.unparse(output_classes[cls_name])
            if 'cancel_all_orders(sym)' in text or "fetch_ohlcv(symbol, '1m'" in text:
                raise PatchError('Unsafe legacy operation remains in replacement class')
        return result, self.changes


def patch_source(source):
    return SourcePatcher(source).render()


def atomic_write(path, content):
    # Replace the path rather than following an old report symlink/hard link.
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temp_name = handle.name
            handle.write(content)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('original', type=Path, help='Your original Python file, saved from the pasted script')
    parser.add_argument('-o', '--output', type=Path, default=Path('apex_30m_fixed.py'))
    parser.add_argument('--force', action='store_true', help='Allow replacing a previous OUTPUT, never the original')
    args = parser.parse_args()
    source_path, output_path = args.original.resolve(), args.output.resolve()
    report_path = output_path.with_suffix('.changes.json')
    if source_path in (output_path, report_path.resolve()):
        parser.error('Do not overwrite the original with output or report. Select a different output path.')
    if output_path.exists() and not args.force:
        parser.error('Output already exists; choose a new name or --force.')
    try:
        result, changes = patch_source(source_path.read_text(encoding='utf-8-sig'))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        report = json.dumps({'version': PATCH_VERSION, 'changes': changes,
                             'validation': 'syntax + structural checks only; no exchange connection'},
                            ensure_ascii=False, indent=2)
        atomic_write(output_path, result)
        atomic_write(report_path, report)
        print('Created:', output_path)
        print('Change report:', report_path)
        print('Original was NOT run/imported/overwritten. No credentials displayed.')
        print('Default mode is DRY_RUN=true. Rotate the exposed credentials before any demo/live test.')
    except (PatchError, SyntaxError, OSError) as exc:
        parser.exit(1, 'Build failed: ' + str(exc) + '\nCheck output/report paths before retrying.\n')


if __name__ == '__main__':
    main()
