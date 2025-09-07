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
    el.readOnly = !!ro;           // don’t disable; we still want value posted
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

    // 1) Reset everything to visible & editable
    [rowSupplier,rowFrom,rowTo,rowQty,rowRate,rowAmt,rowMat,rowBags,rowDC,rowMemo]
      .forEach(function(r){ show(r, true); });
    setReadOnly("qty_kg", false);
    setReadOnly("amount_pkr", false);

    // 2) Apply per-kind
    if (k === "PURCHASE") {
      // user inputs: supplier, bags, rate, dc, memo
      show(rowFrom, false);
      show(rowTo,   false);
      show(rowQty,  false);  // derived, hide
      setReadOnly("qty_kg", true);
      setReadOnly("amount_pkr", true);
      // keep rows visible: supplier, bags, rate, dc, memo, material
      show(rowMat, true);
    } else if (k === "SALE") {
      // from company stock → customer; user inputs qty, rate, to_customer
      show(rowSupplier, false);
      show(rowBags,     false);
      show(rowFrom,     false); // server sets to company stock
      show(rowTo,       true);
      show(rowQty,      true);
      show(rowRate,     true);
      show(rowAmt,      true);  // you may let it auto-calc server-side too
      show(rowDC,       true);
      setReadOnly("qty_kg", false);
      setReadOnly("amount_pkr", false);
    } else if (k === "TRANSFER") {
      // customer → customer; qty only, no money fields
      show(rowSupplier, false);
      show(rowBags,     false);
      show(rowRate,     false);
      show(rowAmt,      false);
      show(rowDC,       false);
      show(rowFrom,     true);
      show(rowTo,       true);
      show(rowQty,      true);
      setReadOnly("qty_kg", false);
    }
  }

  function recalcPurchase() {
    var kindSel = document.getElementById("id_kind");
    if (!kindSel || kindSel.value !== "PURCHASE") return;
    var bags = parseInt(document.getElementById("id_bags_count")?.value || "0", 10);
    var rate = parseInt(document.getElementById("id_rate_pkr")?.value || "0", 10);
    var qty  = bags * 25; // display only; server recomputes on save
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

    toggle();          // initial state on page load
    recalcPurchase();  // prefill when PURCHASE
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
