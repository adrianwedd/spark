import ast
import os
import re

def extract_python(source):
    try:
        ast.parse(source)
        return source
    except SyntaxError:
        pass
    match = re.search(r'<<\s*[\'"]?(PY(?:EOF)?)[\'"]?\s*\n(.*?)\n\1', source, re.DOTALL)
    if match:
        return match.group(2)
    return None

def check_file(path):
    try:
        with open(path, 'r') as f:
            source = f.read()
    except UnicodeDecodeError:
        return None
        
    py_source = extract_python(source)
    if not py_source:
        return None
        
    has_pep604 = False
    pep604_lines = set()
    has_future_annotations = False
    has_isinstance = False
    
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return None
        
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == '__future__' and any(n.name == 'annotations' for n in node.names):
                has_future_annotations = True
                
    class TypeHintVisitor(ast.NodeVisitor):
        def check_node(self, node):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                pep604_lines.add(node.lineno)
                
        def visit_FunctionDef(self, node):
            if node.returns:
                for child in ast.walk(node.returns):
                    self.check_node(child)
            for arg in node.args.args + getattr(node.args, 'kwonlyargs', []) + getattr(node.args, 'posonlyargs', []):
                if arg.annotation:
                    for child in ast.walk(arg.annotation):
                        self.check_node(child)
            self.generic_visit(node)
            
        def visit_AnnAssign(self, node):
            if node.annotation:
                for child in ast.walk(node.annotation):
                    self.check_node(child)
            self.generic_visit(node)

        def visit_Call(self, node):
            nonlocal has_isinstance
            if isinstance(node.func, ast.Name) and node.func.id == 'isinstance':
                if len(node.args) == 2:
                    type_arg = node.args[1]
                    for child in ast.walk(type_arg):
                        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                            has_isinstance = True
            self.generic_visit(node)
            
    visitor = TypeHintVisitor()
    visitor.visit(tree)
    
    if not pep604_lines and not has_isinstance:
        return None
        
    # Check if runs under python3 (direct)
    runs_under_sys_py3 = '/usr/bin/python3' in source or '#!/usr/bin/env python3' in source or 'exec python3' in source or 'python -' in source or 'python3 -' in source
    
    # Or if imported (src/pxh)
    if 'src/pxh' in path:
        runs_under_sys_py3 = True # Assume it can be imported by something running python3
        
    return {
        'file': path,
        'has_future': 'y' if has_future_annotations else 'n',
        'pep604_lines': ', '.join(map(str, sorted(list(pep604_lines)))),
        'runs_under_py3': 'y' if runs_under_sys_py3 else 'n',
        'broken': 'y' if (not has_future_annotations or has_isinstance) else 'n'
    }

dirs = ['bin', 'src/pxh']
results = []
for d in dirs:
    for root, _, files in os.walk(d):
        for f in files:
            path = os.path.join(root, f)
            if f.endswith('.py') or ('bin' in root and not '.' in f):
                res = check_file(path)
                if res:
                    results.append(res)

print("| file | has future import (y/n) | PEP 604 lines found | runs under /usr/bin/python3 (direct or imported) | BROKEN on 3.9? (y/n) |")
print("|---|---|---|---|---|")
for r in sorted(results, key=lambda x: x['file']):
    print(f"| {r['file']} | {r['has_future']} | {r['pep604_lines']} | {r['runs_under_py3']} | {r['broken']} |")
