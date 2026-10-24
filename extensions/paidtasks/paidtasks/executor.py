"""Bounded built-in work only. Never eval/exec customer or generated code."""
import ast
from decimal import Decimal, localcontext, Inexact
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re

MODEL_TASKS = {"explain_problem", "review_code", "generate_small_script"}

def validate_task(task, args):
    if not isinstance(args, dict) or len(json.dumps(args, ensure_ascii=False)) > 12000:
        raise ValueError("task_input_too_large")
    if task == "text_cleanup":
        if set(args) != {"text"} or not isinstance(args["text"], str) or not 1 <= len(args["text"]) <= 4000:
            raise ValueError("text_requires_1_to_4000_characters")
    elif task == "render_plot":
        values = args.get("values")
        labels = args.get("labels")
        if set(args) != {"values", "labels", "unit"} or not isinstance(values, list) or not 1 <= len(values) <= 20:
            raise ValueError("plot_requires_1_to_20_values")
        if not all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1000000 for v in values):
            raise ValueError("plot_values_invalid")
        if not isinstance(labels, list) or len(labels) != len(values) or not all(isinstance(x,str) and len(x)<=12 for x in labels):
            raise ValueError("plot_labels_invalid")
        if not isinstance(args["unit"],str) or len(args["unit"])>20:
            raise ValueError("plot_unit_invalid")
    elif task == "render_formula":
        if set(args) != {"expression"} or not isinstance(args["expression"],str):
            raise ValueError("formula_expression_required")
        arithmetic(args["expression"])
    elif task in MODEL_TASKS:
        if set(args) != {"text"} or not isinstance(args["text"],str) or not 1<=len(args["text"])<=8000:
            raise ValueError("model_input_invalid")
    else:
        raise ValueError("unsupported_task")

def arithmetic(expression):
    # A small arithmetic tree, not Python execution or arbitrary symbolic code.
    if len(expression)>160 or not re.fullmatch(r"[0-9. +*/()\-]+",expression):
        raise ValueError("formula_only_decimal_arithmetic")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 60:
        raise ValueError("formula_too_complex")
    def run(node):
        if isinstance(node,ast.Expression):return run(node.body)
        if isinstance(node,ast.Constant) and type(node.value) in (int,float):
            value=Decimal(ast.get_source_segment(expression,node))
        elif isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
            value=run(node.operand)*(1 if isinstance(node.op,ast.UAdd) else -1)
        elif isinstance(node,ast.BinOp) and isinstance(node.op,(ast.Add,ast.Sub,ast.Mult,ast.Div)):
            a,b=run(node.left),run(node.right)
            if isinstance(node.op,ast.Add):value=a+b
            elif isinstance(node.op,ast.Sub):value=a-b
            elif isinstance(node.op,ast.Mult):value=a*b
            else:
                if b==0:raise ValueError("division_by_zero")
                value=a/b
        else:raise ValueError("formula_operator_not_allowed")
        if not value.is_finite() or abs(value)>Decimal('1e20'):
            raise ValueError("formula_range")
        return value
    return str(run(tree).normalize())

def font(size):
    from PIL import ImageFont
    candidates=("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                str(Path(__file__).resolve().parent.parent/'assets/fonts/NotoSansCJKsc-Regular.otf'),
                "/System/Library/Fonts/STHeiti Medium.ttc",
                "/System/Library/Fonts/PingFang.ttc")
    for name in candidates:
        if Path(name).is_file():return ImageFont.truetype(name,size)
    raise ValueError("render_font_missing")

def png_for(task, args, text):
    from PIL import Image, ImageDraw
    height=220+35*len(args['values']) if task=='render_plot' else 620
    image=Image.new("RGB",(960,height),"white");draw=ImageDraw.Draw(image)
    big,small=font(30),font(21)
    if task == "render_plot":
        draw.text((35,22),"输入数据柱状图 / 单位："+args["unit"],font=big,fill="#14324a")
        values=args["values"];scale=max(max(values),1)
        baseline=height-95
        draw.line((280,90,280,baseline,860,baseline),fill="#14324a",width=2)
        for tick in range(6):
            x=280+tick*108;draw.line((x,90,x,baseline),fill="#e2e8f0")
            draw.text((x-12,baseline+10),f"{scale*tick/5:.2g}",font=small,fill="#333333")
        for i,(label,value) in enumerate(zip(args["labels"],values)):
            y=100+i*35;end=280+value/scale*540
            draw.rectangle((280,y,end,y+22),fill="#337cc2")
            draw.text((15,y),label,font=small,fill="#14324a")
            draw.text((end+6,y),f'{value:.4g}',font=small,fill="#14324a")
        draw.text((35,height-40),"数据来源：本次客户确认的输入；未推断缺失数据",font=small,fill="#333333")
    else:
        draw.text((35,25),"确定性算术核对",font=big,fill="#14324a")
        # Fixed wrapping bounds prevent accidental enormous images.
        for i in range(0,min(len(text),240),35):
            draw.text((35,95+i//35*55),text[i:i+35],font=big,fill="#14324a")
        draw.text((35,550),"仅支持十进制四则运算；除法最多28位有效数字",font=small,fill="#333333")
    buf=io.BytesIO();image.save(buf,format="PNG")
    if len(buf.getvalue())>2*1024*1024:raise ValueError("render_output_too_large")
    return buf.getvalue()

def save_atomic(path, content):
    temp=path.with_suffix(path.suffix+".tmp")
    fd=os.open(temp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,"wb") as f:f.write(content);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)

def execute(order, root, model=None):
    oid=order["id"];q=order["quote"];task=q["task"];args=q["arguments"]
    validate_task(task,args)
    directory=Path(root)/"artifacts"/oid
    directory.mkdir(parents=True,mode=0o700,exist_ok=True)
    if (directory/"result.json").exists():
        raise ValueError("existing_result_requires_recovery_not_regeneration")
    image=None;actual=0
    if task=="text_cleanup":
        lines=args["text"].splitlines()
        cleaned=[line.strip() for line in lines if line.strip()]
        text="\n".join(cleaned)
        checks={"input_lines":len(lines),"output_lines":len(cleaned),"only_trim_and_blank_removal":True}
    elif task=="render_formula":
        with localcontext() as ctx:
            ctx.clear_flags();value=arithmetic(args["expression"]);rounded=ctx.flags[Inexact]
        text=args["expression"]+(" ≈ " if rounded else " = ")+value
        image=png_for(task,args,text);checks={"bounded_arithmetic":True}
    elif task=="render_plot":
        text="单位："+args["unit"]+"\n"+"\n".join(f"{a}: {b}" for a,b in zip(args["labels"],args["values"]))
        image=png_for(task,args,text);checks={"points":len(args["values"]),"unit":args["unit"]}
    elif task in MODEL_TASKS and model:
        response=model(task,args["text"],q["budget_microyuan"],oid)
        text=response["text"]
        if not isinstance(text,str) or not text or len(text)>16000:raise ValueError("model_output_invalid")
        # No trusted usage contract yet: never pretend cost is zero.
        actual=response.get("actual_microyuan")
        checks={"executed_customer_code":False,"usage_known":actual is not None}
        if task=="generate_small_script":
            code=text.removeprefix("```python\n").removesuffix("```").strip()
            ast.parse(code);text=code;checks["python_syntax_valid"]=True
    else:raise ValueError("executor_unavailable")
    body=text.encode();save_atomic(directory/"result.txt",body)
    result={"text":text,"sha256":hashlib.sha256(body).hexdigest(),"text_path":str(directory/"result.txt"),
        "checks":checks,"version":1,"actual_microyuan":actual}
    if image:
        save_atomic(directory/"result.png",image)
        result.update(image_path=str(directory/"result.png"),image_sha256=hashlib.sha256(image).hexdigest())
    save_atomic(directory/"result.json",json.dumps(result,ensure_ascii=False).encode())
    return result

def load_saved_result(root, oid):
    if not re.fullmatch(r"PT[A-F0-9]{16}", oid):
        raise ValueError("order_id_invalid")
    directory = (Path(root)/"artifacts"/oid).resolve()
    manifest = directory/"result.json"
    if manifest.is_symlink() or manifest.stat().st_size > 100000:
        raise ValueError("saved_result_invalid")
    result = json.loads(manifest.read_text())
    for label, filename in (("text", "result.txt"), ("image", "result.png")):
        if label == "image" and "image_path" not in result:
            continue
        p = Path(result[label+"_path"])
        if p.is_symlink() or p.resolve() != directory/filename or p.stat().st_size > 2*1024*1024:
            raise ValueError("saved_result_path")
        expected = result["sha256" if label == "text" else "image_sha256"]
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected or (label == "text" and raw.decode() != result["text"]):
            raise ValueError("saved_result_integrity")
    return result
