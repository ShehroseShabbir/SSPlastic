// core/static/core/admin/raw_material_txn.js
(function () {
  function byField(name) {
    return (
      document.querySelector(".form-row.field-" + name) ||
      document.querySelector(".form-group.field-" + name) ||
      document.querySelector("[data-fieldname='" + name + "']")
    );
  }
  function show(el, on) { if (el) el.style.display = on ? "" : "none"; }
  function setReadOnly(idSuffix, ro) {
    var el = document.getElementById("id_" + idSuffix);
    if (!el) return;
    el.readOnly = !!ro; // keep enabled so value posts
  }

  function toggle() {
    var kindSel = document.getElementById("id_kind");
    if (!kindSel) return;
    var k = kindSel.value || "";

    var rowSupplier = byField("supplier_name");
    var rowFrom     = byField("from_customer");
    var rowTo       = byField("to_customer");
    var rowQty      = byField("qty_kg");
    var rowRate     = byField("rate_pkr");
    var rowAmt      = byField("amount_pkr");
    var rowMat      = byField("material_type");
    var rowBags     = byField("bags_count");
    var rowDC       = byField("dc_number");
    var rowMemo     = byField("memo");

    // Reset: visible & editable
    [rowSupplier,rowFrom,rowTo,rowQty,rowRate,rowAmt,rowMat,rowBags,rowDC,rowMemo]
      .forEach(function(r){ show(r, true); });
    setReadOnly("qty_kg", false);
    setReadOnly("amount_pkr", false);

    if (k === "PURCHASE") {
      // Only bags + rate (+ supplier/DC/memo). Qty/amount derived and hidden.
      show(rowFrom, false);
      show(rowTo,   false);
      show(rowQty,  false);
      show(rowAmt,  false);
      setReadOnly("qty_kg", true);
      setReadOnly("amount_pkr", true);
      show(rowSupplier, true);
      show(rowBags, true);
      show(rowRate, true);
      show(rowDC, true);
      show(rowMemo, true);
      show(rowMat, true);
    } else if (k === "SALE") {
      // Company stock → customer. Enter qty, rate, to_customer.
      show(rowSupplier, false);
      show(rowBags, false);
      show(rowFrom, false); // server sets to company stock
      show(rowTo, true);
      show(rowQty, true);
      show(rowRate, true);
      show(rowAmt, true);
      show(rowDC, true);
      show(rowMemo, true);
      show(rowMat, true);
      setReadOnly("qty_kg", false);
      setReadOnly("amount_pkr", false);
    } else if (k === "TRANSFER") {
      // Customer → customer. Qty only, no money fields.
      show(rowSupplier, false);
      show(rowBags, false);
      show(rowRate, false);
      show(rowAmt, false);
      show(rowDC, false);
      show(rowFrom, true);
      show(rowTo, true);
      show(rowQty, true);
      show(rowMemo, true);
      show(rowMat, true);
      setReadOnly("qty_kg", false);
    }
  }

  function recalcPurchase() {
    var kindSel = document.getElementById("id_kind");
    if (!kindSel || kindSel.value !== "PURCHASE") return;
    var bags = parseInt(document.getElementById("id_bags_count")?.value || "0", 10);
    var rate = parseInt(document.getElementById("id_rate_pkr")?.value || "0", 10);
    var qty  = bags * 25; // 25 kg per bag (display only)
    var amt  = qty * rate;
    var qtyInput = document.getElementById("id_qty_kg");
    var amtInput = document.getElementById("id_amount_pkr");
    if (qtyInput) qtyInput.value = qty.toFixed(3);
    if (amtInput) amtInput.value = String(amt);
  }

  function bind() {
    var kindSel = document.getElementById("id_kind");
    if (!kindSel) return;
    kindSel.addEventListener("change", function () {
      toggle();
      recalcPurchase();
    });

    var bags = document.getElementById("id_bags_count");
    var rate = document.getElementById("id_rate_pkr");
    if (bags) bags.addEventListener("input", recalcPurchase);
    if (rate) rate.addEventListener("input", recalcPurchase);

    toggle();
    recalcPurchase();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
