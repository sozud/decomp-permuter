import sys
import random
import copy

from pycparser import c_ast, c_parser, c_generator

class PatchedCGenerator(c_generator.CGenerator):
    """Like a CGenerator, except it keeps else if's prettier despite
    the terrible things we've done to them in normalize_ast."""
    def visit_If(self, n):
        n2 = n
        if (n.iffalse and isinstance(n.iffalse, c_ast.Compound) and
                len(n.iffalse.block_items or []) == 1 and
                isinstance(n.iffalse.block_items[0], c_ast.If)):
            n2 = c_ast.If(cond=n.cond, iftrue=n.iftrue,
                    iffalse=n.iffalse.block_items[0])
        return super().visit_If(n2)

def to_c(ast):
    source = PatchedCGenerator().visit(ast)
    if '#pragma' not in source:
        return source
    lines = source.split('\n')
    out = []
    same_line = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('#pragma'):
            if stripped == '#pragma sameline start':
                same_line += 1
                continue
            elif stripped == '#pragma sameline end':
                same_line -= 1
                if same_line == 0:
                    out.append('\n')
                continue
        if not same_line:
            line += '\n'
        out.append(line)
    assert same_line == 0
    return ''.join(out).rstrip() + '\n'

def find_fns(ast):
    ret = []
    for node in ast.ext:
        if isinstance(node, c_ast.FuncDef):
            ret.append(node)
    return ret

def visit_subexprs(top_node, callback):
    def rec(node, toplevel=False):
        assert node is not None
        TODO = False # issue #12: process loop header fields (as soon as there's a point in doing so)
        if isinstance(node, c_ast.Assignment):
            node.rvalue = rec(node.rvalue)
        elif isinstance(node, c_ast.StructRef):
            node.name = rec(node.name)
        elif isinstance(node, (c_ast.Return, c_ast.Cast)):
            if node.expr:
                node.expr = rec(node.expr)
        elif isinstance(node, (c_ast.Constant, c_ast.ID)):
            if not toplevel:
                x = callback(node)
                if x: return x
        elif isinstance(node, c_ast.UnaryOp):
            if not toplevel:
                x = callback(node)
                if x: return x
            if node.op not in ['p++', 'p--', '++', '--', '&']:
                node.expr = rec(node.expr)
        elif isinstance(node, c_ast.BinaryOp):
            if not toplevel:
                x = callback(node)
                if x: return x
            node.left = rec(node.left)
            node.right = rec(node.right)
        elif isinstance(node, c_ast.Compound):
            for sub in node.block_items or []:
                rec(sub, True)
        elif isinstance(node, (c_ast.Case, c_ast.Default)):
            for sub in node.stmts or []:
                rec(sub, True)
        elif isinstance(node, c_ast.FuncCall):
            if not toplevel:
                x = callback(node)
                if x: return x
            if node.args:
                rec(node.args, True)
        elif isinstance(node, c_ast.ExprList):
            if not toplevel:
                x = callback(node)
                if x: return x
            for i in range(len(node.exprs)):
                node.exprs[i] = rec(node.exprs[i])
        elif isinstance(node, c_ast.ArrayRef):
            if not toplevel:
                x = callback(node)
                if x: return x
            node.name = rec(node.name)
            node.subscript = rec(node.subscript)
        elif isinstance(node, c_ast.Decl):
            if node.init:
                node.init = rec(node.init)
        elif isinstance(node, c_ast.For):
            if node.init:
                node.init = rec(node.init)
            if node.cond:
                if TODO: node.cond = rec(node.cond)
            if node.next:
                node.next = rec(node.next, True)
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, c_ast.TernaryOp):
            if not toplevel:
                x = callback(node)
                if x: return x
            node.cond = rec(node.cond)
            node.iftrue = rec(node.iftrue)
            node.iffalse = rec(node.iffalse)
        elif isinstance(node, c_ast.While):
            if TODO: node.cond = rec(node.cond)
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, c_ast.DoWhile):
            node.stmt = rec(node.stmt, True)
            if TODO: node.cond = rec(node.cond)
        elif isinstance(node, c_ast.Switch):
            node.cond = rec(node.cond)
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, c_ast.Label):
            node.stmt = rec(node.stmt, True)
        elif isinstance(node, c_ast.If):
            node.cond = rec(node.cond)
            node.iftrue = rec(node.iftrue, True)
            if node.iffalse:
                node.iffalse = rec(node.iffalse, True)
        elif isinstance(node, (c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl,
                c_ast.Typename, c_ast.EmptyStatement, c_ast.Pragma, c_ast.Break,
                c_ast.Continue, c_ast.Goto)):
            pass
        else:
            print("Node with unknown type!", file=sys.stderr)
            print(node, file=sys.stderr)
            exit(1)
        return node

    rec(top_node, True)

def get_block_stmts(block, force):
    if isinstance(block, c_ast.Compound):
        ret = block.block_items or []
        if force and not block.block_items:
            block.block_items = ret
    else:
        assert isinstance(block, (c_ast.Case, c_ast.Default))
        ret = block.stmts or []
        if force and not block.stmts:
            block.stmts = ret
    return ret

def insert_decl(fn, decl):
    for index, stmt in enumerate(fn.body.block_items):
        if not isinstance(stmt, c_ast.Decl):
            break
    else:
        index = len(fn.body.block_items)
    fn.body.block_items[index:index] = [decl]

def insert_statement(block, index, stmt):
    stmts = get_block_stmts(block, True)
    stmts[index:index] = [stmt]

def brace_nested_blocks(stmt):
    def brace(stmt):
        if isinstance(stmt, (c_ast.Compound, c_ast.Case, c_ast.Default)):
            return stmt
        return c_ast.Compound([stmt])
    if isinstance(stmt, (c_ast.For, c_ast.While, c_ast.DoWhile)):
        stmt.stmt = brace(stmt.stmt)
    elif isinstance(stmt, c_ast.If):
        stmt.iftrue = brace(stmt.iftrue)
        if stmt.iffalse:
            stmt.iffalse = brace(stmt.iffalse)
    elif isinstance(stmt, c_ast.Switch):
        stmt.stmt = brace(stmt.stmt)
    elif isinstance(stmt, c_ast.Label):
        brace_nested_blocks(stmt.stmt)

def for_nested_blocks(stmt, callback):
    if isinstance(stmt, c_ast.Compound):
        callback(stmt)
    elif isinstance(stmt, (c_ast.For, c_ast.While, c_ast.DoWhile)):
        callback(stmt.stmt)
    elif isinstance(stmt, c_ast.If):
        if stmt.iftrue:
            callback(stmt.iftrue)
        if stmt.iffalse:
            callback(stmt.iffalse)
    elif isinstance(stmt, c_ast.Switch):
        callback(stmt.stmt)
    elif isinstance(stmt, (c_ast.Case, c_ast.Default)):
        callback(stmt)
    elif isinstance(stmt, c_ast.Label):
        for_nested_blocks(stmt.stmt, callback)

def perm_temp_for_expr(fn):
    phase = 0
    einds = {}
    sumprob = 0
    targetprob = None
    found = None
    def rec(block, reuse_cands):
        stmts = get_block_stmts(block, False)
        reuse_cands = reuse_cands[:]
        assignment_cands = []
        past_decls = False
        for index, stmt in enumerate(stmts):
            if isinstance(stmt, c_ast.Decl):
                if not isinstance(stmt.type, c_ast.ArrayDecl):
                    reuse_cands.append(stmt.name)
                    if not isinstance(stmt.type, c_ast.PtrDecl):
                        # Make non-pointers more common
                        reuse_cands.append(stmt.name)
            else:
                past_decls = True
            if past_decls:
                assignment_cands.append((block, index))

            for_nested_blocks(stmt, lambda b: rec(b, reuse_cands))

            def visitor(expr):
                nonlocal sumprob
                nonlocal found
                eind = einds.get(id(expr), 0)
                for place in assignment_cands[::-1]:
                    prob = 1 / (1 + eind)
                    if isinstance(expr, (c_ast.ID, c_ast.Constant)):
                        prob *= 0.5
                    sumprob += prob
                    if phase == 1 and found is None and sumprob > targetprob:
                        if random.randint(0,1) or not reuse_cands:
                            var = c_ast.ID('new_var')
                            reused = False
                        else:
                            var = c_ast.ID(random.choice(reuse_cands))
                            reused = True
                        found = (place, expr, var, reused)
                        return var
                    eind += 1
                einds[id(expr)] = eind
                return None
            visit_subexprs(stmt, visitor)
        assignment_cands.append((block, len(stmts)))

    rec(fn.body, [])
    phase = 1
    targetprob = random.uniform(0, sumprob)
    sumprob = 0
    einds = {}
    rec(fn.body, [])

    assert found is not None
    location, expr, var, reused = found
    # print("replacing:", to_c(expr))
    block, index = location
    assignment = c_ast.Assignment('=', var, expr)
    insert_statement(block, index, assignment)
    if not reused:
        typ = c_ast.TypeDecl(declname=var.name, quals=[],
                type=c_ast.IdentifierType(names=['int']))
        decl = c_ast.Decl(name=var.name, quals=[], storage=[], funcspec=[],
                type=typ, init=None, bitsize=None)
        insert_decl(fn, decl)

def perm_sameline(fn):
    cands = []
    def rec(block):
        stmts = get_block_stmts(block, False)
        for index, stmt in enumerate(stmts):
            cands.append((block, index))
            for_nested_blocks(stmt, rec)
        cands.append((block, len(stmts)))
    rec(fn.body)
    n = len(cands)
    assert n >= 3
    # Generate a small random interval
    le = n - 2
    for i in range(4):
        le *= random.uniform(0, 1)
    le = int(le) + 2
    i = random.randrange(n - le)
    j = i + le
    # Insert the second statement first, since inserting a statement may cause
    # later indices to move.
    insert_statement(cands[j][0], cands[j][1], c_ast.Pragma("sameline end"))
    insert_statement(cands[i][0], cands[i][1], c_ast.Pragma("sameline start"))

def normalize_ast(ast):
    # Add braces to all ifs/fors/etc., to make it easier to insert statements.
    fn = find_fns(ast)[0]
    def rec(block):
        stmts = get_block_stmts(block, False)
        for stmt in stmts:
            brace_nested_blocks(stmt)
            for_nested_blocks(stmt, rec)
    rec(fn.body)

class Randomizer():
    def __init__(self, start_ast):
        self.start_ast = start_ast
        normalize_ast(self.start_ast)
        self.ast = self.start_ast

    def get_current_source(self):
        return to_c(self.ast)

    def randomize(self):
        ast = copy.deepcopy(self.start_ast)
        fn = find_fns(ast)[0]
        methods = [
            (perm_temp_for_expr, 90),
            #(perm_sameline, 10),
        ]
        method = random.choice([x for (elem, prob) in methods for x in [elem]*prob])
        method(fn)
        self.ast = ast