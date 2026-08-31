import { readApiToken } from "../auth/tokenStorage";
export type BacktestRun = { id:string; run_kind:string; status:string; progress:number; current_date?:string|null; current_step?:number|null; message?:string };
async function request(path:string, init:RequestInit={}) { const r=await fetch(path,{...init,headers:{"Content-Type":"application/json",Authorization:`Bearer ${readApiToken()||""}`,...init.headers}}); if(!r.ok) throw new Error((await r.json().catch(()=>({}))).detail||"回测请求失败"); return r.json(); }
export const listBacktestRuns=()=>request("/api/admin/backtest-runs");
export const createBacktestRun=(payload:unknown)=>request("/api/admin/backtest-runs",{method:"POST",body:JSON.stringify(payload)});
export const getBacktestRun=(id:string)=>request(`/api/admin/backtest-runs/${id}`);
export const cancelBacktestRun=(id:string)=>request(`/api/admin/backtest-runs/${id}/cancel`,{method:"POST"});
