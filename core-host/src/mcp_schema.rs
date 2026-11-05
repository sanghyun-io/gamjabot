//! MCP same-document reference policy; never resolves an external resource.
use serde_json::Value;
use std::collections::{HashMap,HashSet};
const MAX_DEPTH:usize=64;
const MAX_EXPANSION:usize=131_072;
const DIALECT:&str="https://json-schema.org/draft/2020-12/schema";
const DRAFT7:&str="http://json-schema.org/draft-07/schema#";

pub(crate) fn safe_mcp_schema(root:&Value)->bool {
    if !root.is_object() {return false;}
    let dialect=match root.get("$schema") {None=>DIALECT,Some(Value::String(s)) if s==DIALECT || s==DRAFT7=>s.as_str(),_=>return false};
    let mut stack=vec![(root,0usize)]; let mut has_refs=false; let mut forbidden=false; let mut nodes=0usize;
    while let Some((value,depth))=stack.pop() {
        nodes+=1; if depth>MAX_DEPTH || nodes>MAX_EXPANSION {return false;}
        match value {
            Value::Object(map)=>{
                if map.contains_key("$dynamicRef") || map.contains_key("$recursiveRef") {return false;}
                has_refs |= map.contains_key("$ref");
                forbidden |= ["$id","$anchor","$dynamicAnchor","$dynamicRef","$recursiveRef","$recursiveAnchor"].iter().any(|k|map.contains_key(*k));
                if map.get("$schema").is_some_and(|v|v.as_str()!=Some(dialect)) {return false;}
                stack.extend(map.values().map(|v|(v,depth+1)));
            },
            Value::Array(items)=>stack.extend(items.iter().map(|v|(v,depth+1))),
            _=>{},
        }
    }
    if !has_refs {return true;}
    if forbidden {return false;}
    fn pointer<'a>(root:&'a Value, reference:&str)->Option<&'a Value> {
        if !reference.starts_with("#/") || reference.contains('%') {return None;}
        let tokens=reference[2..].split('/'); if tokens.clone().count()>MAX_DEPTH {return None;}
        for token in tokens {
            let mut chars=token.chars();
            while let Some(c)=chars.next() {if c=='~' && !matches!(chars.next(),Some('0'|'1')) {return None;}}
        }
        let found=root.pointer(&reference[1..])?;
        if found.is_object() || found.is_boolean() {Some(found)} else {None}
    }
    fn visit<'a>(root:&'a Value,v:&'a Value,depth:usize,active:&mut HashSet<usize>,memo:&mut HashMap<usize,(usize,usize)>)->Option<(usize,usize)> {
        if depth>MAX_DEPTH {return None;}
        if !v.is_object() && !v.is_array() {return Some((1,0));}
        let key=v as *const Value as usize;
        if active.contains(&key) {return None;}
        if let Some(cached)=memo.get(&key) {return if depth.checked_add(cached.1)?<=MAX_DEPTH {Some(*cached)} else {None};}
        active.insert(key);let mut cost=1usize;let mut height=0usize;
        let mut add=|child:&'a Value,active:&mut HashSet<usize>,memo:&mut HashMap<usize,(usize,usize)>| -> Option<()> {
            let (sub,subheight)=visit(root,child,depth+1,active,memo)?;
            cost=cost.checked_add(sub)?; height=height.max(subheight.checked_add(1)?);
            if cost>MAX_EXPANSION || height>MAX_DEPTH {return None;} Some(())
        };
        match v {
            Value::Object(map)=>{
                for child in map.values() {add(child,active,memo)?;}
                if let Some(reference)=map.get("$ref") {add(pointer(root,reference.as_str()?)?,active,memo)?;}
            },
            Value::Array(items)=>for child in items {add(child,active,memo)?;},
            _=>{},
        }
        active.remove(&key);memo.insert(key,(cost,height));Some((cost,height))
    }
    visit(root,root,0,&mut HashSet::new(),&mut HashMap::new()).is_some()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn bounded_static_pointer_graph_policy() {
        let good=json!({"type":"object","$defs":{"a":{"$ref":"#/$defs/b"},"b":{"type":"string"},"a/b":{"type":"integer"}},"properties":{"v":{"$ref":"#/$defs/a"},"w":{"$ref":"#/$defs/a~1b"}}});
        assert!(safe_mcp_schema(&good));
        let mut draft7=good.clone();draft7.as_object_mut().unwrap().insert("$schema".into(),json!(DRAFT7));
        assert!(safe_mcp_schema(&draft7));
        let mut mismatch=draft7.clone();mismatch["$defs"]["b"]["$schema"]=json!(DIALECT);
        assert!(!safe_mcp_schema(&mismatch));
        assert!(!safe_mcp_schema(&json!({"type":"object","$schema":"https://unknown.invalid/schema"})));
        assert!(safe_mcp_schema(&json!({"type":"object","$schema":DRAFT7,"properties":{"x":{"type":"string"}}})));

        for bad in [json!({"$ref":"https://example.invalid/schema"}),json!({"$ref":"#/$defs/missing"}),json!({"$defs":{"a":{"$ref":"#/$defs/b"},"b":{"$ref":"#/$defs/a"}},"$ref":"#/$defs/a"}),json!({"$id":"https://example.invalid/","$ref":"#/x","x":{}}),json!({"$dynamicRef":"#/x","x":{}}),json!({"$ref":"#/%24defs/x"}),json!({"$ref":"#/bad~2escape"})] {assert!(!safe_mcp_schema(&bad));}
        let mut defs=serde_json::Map::new();defs.insert("leaf".into(),json!({"type":"string"}));
        for i in (0..19).rev() {let next=if i==18 {"leaf".to_string()} else {format!("x{}",i+1)};
            let reference=format!("#/$defs/{next}");defs.insert(format!("x{i}"),json!({"allOf":[{"$ref":reference},{"$ref":reference}]}));}
        assert!(!safe_mcp_schema(&json!({"type":"object","$defs":defs,"properties":{"x":{"$ref":"#/$defs/x0"}}})));
        assert!(safe_mcp_schema(&json!({"type":"object","$defs":{"a~b":{"type":"string"}},"properties":{"x":{"$ref":"#/$defs/a~0b"}}})));

    }
}
