/** @typedef {{id:number,name:string,initial:object,records:object[],description:string}} LogSet */
/** @typedef {{id:number,name:string,log_set_id:number,crash_after:number}} CrashCase */
/** @typedef {{id:number,crash_case_id:number,label:string,status:string,snapshot:object,result:object|null}} Recovery */
/** @typedef {{id:number,log_set_id:number,recovery_id:number,version_no:number,state:string,ledger:object|null,published_at:string|null,last_error:string|null,attempts:number}} LedgerVersion */
/** @typedef {{point_no:number,point_key:string,value:number}} LedgerPoint */
export {};
