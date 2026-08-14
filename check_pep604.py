import ast
import os
import re

def extract_python(source):
    # Try parsing as-is
    try:
        ast.parse(source)
        return source
    except SyntaxError:
        pass
    
    # Try finding heredoc
    match = re.search(r'<<\s*[\'"]?(PY(?:EOF)?)[\'"]?\s*\n(.*?)\n\1', source, re.DOTALL)
    if match:
        return match.group(2)
        
    return None

def check_file(path):
    try:
        with open(path, 'r') as f:
            source = f.read()
    except UnicodeDecodeError:
        return
        
    py_source = extract_python(source)
    if not py_source:
        return
        
    has_pep604 = False
    pep604_lines = []
    has_future_annotations = False
    
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return
        
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == '__future__' and any(n.name == 'annotations' for n in node.names):
                has_future_annotations = True
                
    class TypeHintVisitor(ast.NodeVisitor):
        def __init__(self):
            self.lines = set()
            self.has_isinstance = False
            
        def check_node(self, node):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                self.lines.add(node.lineno)
                
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
            if isinstance(node.func, ast.Name) and node.func.id == 'isinstance':
                if len(node.args) == 2:
                    type_arg = node.args[1]
                    for child in ast.walk(type_arg):
                        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
                            self.has_isinstance = True
            self.generic_visit(node)
            
    visitor = TypeHintVisitor()
    visitor.visit(tree)
    
    if visitor.lines or visitor.has_isinstance:
        print(f"FILE: {path}")
        print(f"FUTURE: {has_future_annotations}")
        print(f"LINES: {sorted(list(visitor.lines))}")
        print(f"ISINSTANCE: {visitor.has_isinstance}")

def main():
    dirs = ['bin', 'src/pxh']
    for d in dirs:
        for root, _, files in os.walk(d):
            for f in files:
                if f.endswith('.py') or ('bin' in root and not '.' in f):
                    check_file(os.path.join(root, f))

if __name__ == '__main__':
    main()
