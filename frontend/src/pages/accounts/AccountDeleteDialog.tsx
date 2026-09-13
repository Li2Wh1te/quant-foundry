import { useEffect, useRef, useState } from "react";
import { LoaderCircle, X } from "lucide-react";
import { checkAccountDeletion, getAccountProfile, permanentlyDeleteAccount, updateAccountProfile, type AccountDeletionCheck, type AccountProfile } from "../../api/accountProfiles";

export function AccountDeleteDialog({ account, onClose, onDeleted, onRetired }: {
  account: AccountProfile; onClose: () => void; onDeleted: (id: string) => void; onRetired: (profile: AccountProfile) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null), pending = useRef(false);
  const [profile, setProfile] = useState(account), [check, setCheck] = useState<AccountDeletionCheck | null>(null);
  const [loading, setLoading] = useState(true), [saving, setSaving] = useState(false), [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const element = dialog.current!, previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    element.showModal();
    element.querySelector<HTMLElement>("[data-cancel]")?.focus();
    return () => { element.close(); document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setCheck(null); setError(null);
    // Eligibility is advisory; deletion itself rechecks every reference under
    // a lock. Reload the editor version too, then require a fresh confirmation.
    Promise.all([checkAccountDeletion(account.id, controller.signal), getAccountProfile(account.id)]).then(([eligibility, latest]) => {
      if (!controller.signal.aborted) { setCheck(eligibility); setProfile(latest); }
    }).catch(caught => { if (!controller.signal.aborted) setError(caught instanceof Error ? caught.message : "检查删除条件失败。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [account.id, attempt]);
  async function submit() {
    if (!check || pending.current || loading) return;
    pending.current = true; setSaving(true); setError(null);
    try {
      if (check.can_permanently_delete) { await permanentlyDeleteAccount(profile.id, profile.version); onDeleted(profile.id); }
      else { const retired = await updateAccountProfile(profile.id, { status: "retired", expected_version: profile.version }); onRetired(retired); }
    } catch (caught) {
      // Never retry an irreversible action automatically after a conflict or
      // uncertain response. Reload eligibility and ask for confirmation again.
      setError(caught instanceof Error ? caught.message : "操作失败，请重新检查账户状态。"); setCheck(null);
    } finally { pending.current = false; setSaving(false); }
  }
  const close = () => { if (!pending.current) onClose(); };
  return <dialog ref={dialog} className="qfac-dialog qfac-delete-dialog" aria-modal="true" aria-labelledby="qfac-delete-title" onCancel={event => { event.preventDefault(); close(); }} onKeyDown={event => {
    if (event.key !== "Tab") return;
    const buttons = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
    const first = buttons[0], last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  }}>
    <header className="qfac-modal-head"><h2 id="qfac-delete-title">{check && !check.can_permanently_delete ? "账户已有回测引用" : "删除回测账户"}</h2><button type="button" className="qfac-icon" aria-label="关闭删除确认" disabled={saving} onClick={close}><X aria-hidden="true" /></button></header>
    <div className="qfac-modal-body"><p className="qfac-delete-name">{profile.name}</p><p className="qfac-help qfac-break">账户 UUID：{profile.id} · v{profile.version}</p>
      {loading ? <p role="status">正在检查所有回测引用…</p> : check ? check.can_permanently_delete ? <p>此账户没有回测引用。永久删除会移除账户及全部历史配置版本，<strong>无法恢复</strong>。</p> : <><p>{check.reason ?? "已有回测引用，不能永久删除。"}</p><p>{profile.status === "retired" ? "该账户已退役，历史配置和回测记录将继续保留。" : "可以将账户退役，禁止用于新的回测，已有配置和历史回测保持不变。"}</p></> : null}
      {error && <div className="qfac-error" role="alert">{error}</div>}
    </div>
    <footer className="qfac-modal-footer qfac-actions"><button data-cancel className="qfac-button" type="button" disabled={saving} onClick={close}>取消</button>
      {!check && !loading && <button className="qfac-button" type="button" disabled={saving} onClick={() => setAttempt(value => value + 1)}>重新检查</button>}
      {check && (check.can_permanently_delete || profile.status !== "retired") && <button className="qfac-button qfac-danger" type="button" disabled={saving || loading} onClick={() => void submit()}>{saving && <LoaderCircle className="spin" aria-hidden="true" />}{saving ? "处理中…" : check.can_permanently_delete ? "永久删除" : "确认退役"}</button>}
    </footer>
  </dialog>;
}
