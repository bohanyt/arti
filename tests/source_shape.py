"""Version-independent Python source guards for the two voice flag-off paths.

The fixed v1 syntax schema below includes statement order, names, operators,
arguments, decorators, annotations and literal values. Source positions are not
behavior. Python 3.12's *empty* type_params is equivalent to 3.11's absence;
nonempty parameters and unknown syntax fail closed instead of being discarded.
This is test infrastructure, never imported by the application.
"""
import ast
import hashlib
import json
from pathlib import Path

SCHEMA = 'python-source-shape-v1'
# Explicit fields, not ast.iter_fields/_fields: interpreter schema additions do
# not silently alter a baseline or erase a statement from the guarded path.
FIELDS = {
    'Add': (),
    'And': (),
    'AnnAssign': ('target', 'annotation', 'value', 'simple'),
    'Assert': ('test', 'msg'),
    'Assign': ('targets', 'value', 'type_comment'),
    'AsyncFor': ('target', 'iter', 'body', 'orelse', 'type_comment'),
    'AsyncFunctionDef': ('name', 'args', 'body', 'decorator_list', 'returns', 'type_comment'),
    'AsyncWith': ('items', 'body', 'type_comment'),
    'Attribute': ('value', 'attr', 'ctx'),
    'AugAssign': ('target', 'op', 'value'),
    'AugLoad': (),
    'AugStore': (),
    'Await': ('value',),
    'BinOp': ('left', 'op', 'right'),
    'BitAnd': (),
    'BitOr': (),
    'BitXor': (),
    'BoolOp': ('op', 'values'),
    'Break': (),
    'Call': ('func', 'args', 'keywords'),
    'ClassDef': ('name', 'bases', 'keywords', 'body', 'decorator_list'),
    'Compare': ('left', 'ops', 'comparators'),
    'Constant': ('value', 'kind'),
    'Continue': (),
    'Del': (),
    'Delete': ('targets',),
    'Dict': ('keys', 'values'),
    'DictComp': ('key', 'value', 'generators'),
    'Div': (),
    'Eq': (),
    'ExceptHandler': ('type', 'name', 'body'),
    'Expr': ('value',),
    'Expression': ('body',),
    'FloorDiv': (),
    'For': ('target', 'iter', 'body', 'orelse', 'type_comment'),
    'FormattedValue': ('value', 'conversion', 'format_spec'),
    'FunctionDef': ('name', 'args', 'body', 'decorator_list', 'returns', 'type_comment'),
    'FunctionType': ('argtypes', 'returns'),
    'GeneratorExp': ('elt', 'generators'),
    'Global': ('names',),
    'Gt': (),
    'GtE': (),
    'If': ('test', 'body', 'orelse'),
    'IfExp': ('test', 'body', 'orelse'),
    'Import': ('names',),
    'ImportFrom': ('module', 'names', 'level'),
    'In': (),
    'Interactive': ('body',),
    'Invert': (),
    'Is': (),
    'IsNot': (),
    'JoinedStr': ('values',),
    'LShift': (),
    'Lambda': ('args', 'body'),
    'List': ('elts', 'ctx'),
    'ListComp': ('elt', 'generators'),
    'Load': (),
    'Lt': (),
    'LtE': (),
    'MatMult': (),
    'Match': ('subject', 'cases'),
    'MatchAs': ('pattern', 'name'),
    'MatchClass': ('cls', 'patterns', 'kwd_attrs', 'kwd_patterns'),
    'MatchMapping': ('keys', 'patterns', 'rest'),
    'MatchOr': ('patterns',),
    'MatchSequence': ('patterns',),
    'MatchSingleton': ('value',),
    'MatchStar': ('name',),
    'MatchValue': ('value',),
    'Mod': (),
    'Module': ('body', 'type_ignores'),
    'Mult': (),
    'Name': ('id', 'ctx'),
    'NamedExpr': ('target', 'value'),
    'Nonlocal': ('names',),
    'Not': (),
    'NotEq': (),
    'NotIn': (),
    'Or': (),
    'Param': (),
    'Pass': (),
    'Pow': (),
    'RShift': (),
    'Raise': ('exc', 'cause'),
    'Return': ('value',),
    'Set': ('elts',),
    'SetComp': ('elt', 'generators'),
    'Slice': ('lower', 'upper', 'step'),
    'Starred': ('value', 'ctx'),
    'Store': (),
    'Sub': (),
    'Subscript': ('value', 'slice', 'ctx'),
    'Try': ('body', 'handlers', 'orelse', 'finalbody'),
    'TryStar': ('body', 'handlers', 'orelse', 'finalbody'),
    'Tuple': ('elts', 'ctx'),
    'TypeIgnore': ('tag',),
    'UAdd': (),
    'USub': (),
    'UnaryOp': ('op', 'operand'),
    'While': ('test', 'body', 'orelse'),
    'With': ('items', 'body', 'type_comment'),
    'Yield': ('value',),
    'YieldFrom': ('value',),
    'alias': ('name', 'asname'),
    'arg': ('arg', 'annotation', 'type_comment'),
    'arguments': ('posonlyargs', 'args', 'vararg', 'kwonlyargs', 'kw_defaults', 'kwarg', 'defaults'),
    'boolop': (),
    'cmpop': (),
    'comprehension': ('target', 'iter', 'ifs', 'is_async'),
    'excepthandler': (),
    'expr': (),
    'expr_context': (),
    'keyword': ('arg', 'value'),
    'match_case': ('pattern', 'guard', 'body'),
    'mod': (),
    'operator': (),
    'pattern': (),
    'slice': (),
    'stmt': (),
    'type_ignore': (),
    'unaryop': (),
    'withitem': ('context_expr', 'optional_vars'),
}


def shape(value):
    """Serialize only the fixed syntax contract; no ast.dump or source spelling."""
    if isinstance(value, ast.AST):
        name = type(value).__name__
        if name not in FIELDS:
            raise AssertionError('unsupported source syntax: ' + name)
        if getattr(value, 'type_params', ()):
            raise AssertionError('nonempty type parameters require a source guard schema revision')
        return [name, [[field, shape(getattr(value, field))] for field in FIELDS[name]]]
    if isinstance(value, list):
        return ['list', [shape(item) for item in value]]
    if value is None or type(value) in (str, bool, int):
        return [type(value).__name__, value]
    if type(value) is float:
        return ['float', value.hex()]
    if type(value) is complex:
        return ['complex', value.real.hex(), value.imag.hex()]
    if type(value) is bytes:
        return ['bytes', value.hex()]
    if value is Ellipsis:
        return ['ellipsis']
    raise AssertionError('unsupported source value: ' + type(value).__name__)


def digest(tree):
    encoded = json.dumps([SCHEMA, shape(tree)], ensure_ascii=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()


def baselines():
    rows = json.loads((Path(__file__).parent / 'fixtures/live7/fix121b_contract_baselines.json').read_text(encoding='utf-8'))
    assert rows['source_shape']['schema'] == SCHEMA
    return rows['source_shape']
