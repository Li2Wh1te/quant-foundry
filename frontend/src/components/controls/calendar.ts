export function dateKey(date:Date){return date.toISOString().slice(0,10);}
export function todayKey(){const parts=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());return ['year','month','day'].map(type=>parts.find(p=>p.type===type)!.value).join('-');}
export function calendarDays(month:string){const first=new Date(`${month}-01T00:00:00Z`);first.setUTCDate(1-first.getUTCDay());return Array.from({length:42},(_,index)=>{const date=new Date(first);date.setUTCDate(first.getUTCDate()+index);return dateKey(date);});}
export function shiftMonth(month:string,amount:number){const date=new Date(`${month}-01T00:00:00Z`);date.setUTCMonth(date.getUTCMonth()+amount);return dateKey(date).slice(0,7);}
