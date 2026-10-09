// 報價表單的小幫手。沒有 JavaScript 時表單照常可用，這裡只是讓輸入更快。
(function () {
  'use strict';

  var form = document.getElementById('quotation-form');
  if (!form) return;
  var field = function (name) { return form.elements[name]; };
  var productBox = field('product');

  // ---- 送出：產生 PDF 要幾秒鐘，避免重複送出；按「上一頁」回來時恢復按鈕 ----
  var submitButton = document.getElementById('submit-button');
  form.addEventListener('submit', function () {
    rememberSellerDefaults();
    submitButton.disabled = true;
    submitButton.textContent = '產生中…';
  });
  window.addEventListener('pageshow', function () {
    submitButton.disabled = false;
    submitButton.textContent = '產生報價單';
  });

  // ---- 即時試算：交給伺服器用同一套程式計算，網頁不另外算一份 ----
  var preview = document.getElementById('preview');
  var previewSeq = 0;
  var previewTimer = null;

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(updatePreview, 400);
  }

  function updatePreview() {
    if (!productBox.value.trim()) {
      preview.hidden = true;
      return;
    }
    var seq = ++previewSeq;
    var body = new FormData();
    body.append('product', productBox.value);
    body.append('tax', field('tax').value);
    fetch(form.dataset.previewUrl, { method: 'POST', body: body, credentials: 'same-origin' })
      .then(function (res) { if (!res.ok) throw new Error(res.status); return res.json(); })
      .then(function (data) {
        if (seq !== previewSeq) return;  // 已經有更新的輸入，丟掉舊的結果
        var list = document.getElementById('preview-errors');
        list.replaceChildren.apply(list, data.errors.map(function (message) {
          var li = document.createElement('li');
          li.textContent = message;
          return li;
        }));
        document.getElementById('preview-subtotal').textContent = data.subtotal;
        document.getElementById('preview-tax-label').textContent = data.tax_included ? '內含稅額（5%）' : '稅額（5%）';
        document.getElementById('preview-tax').textContent = data.tax;
        document.getElementById('preview-total').textContent = data.total;
        preview.hidden = false;
      })
      .catch(function () { if (seq === previewSeq) preview.hidden = true; });
  }

  productBox.addEventListener('input', schedulePreview);
  field('tax').addEventListener('change', schedulePreview);
  if (productBox.value.trim()) updatePreview();

  // ---- 統編：帶出上次報價或經濟部登記的資料，只填空白的欄位（手動輸入的優先） ----
  var taxId = field('taxid');
  var taxIdHelp = document.getElementById('taxid-help');
  var customerPrices = {};
  var lastLookup = '';

  function fillIfEmpty(name, value) {
    var input = field(name);
    if (input && value && !input.value.trim()) input.value = value;
  }

  function lookupCustomer() {
    var value = taxId.value.normalize('NFKC').trim();
    if (!/^[0-9]{8}$/.test(value) || value === lastLookup) return;
    lastLookup = value;
    taxIdHelp.textContent = '查詢中…';
    fetch(form.dataset.customerUrl + '?taxid=' + encodeURIComponent(value), { credentials: 'same-origin' })
      .then(function (res) { return res.json().then(function (data) { return { ok: res.ok, data: data }; }); })
      .then(function (result) {
        if (taxId.value.normalize('NFKC').trim() !== value) return;
        var data = result.data;
        customerPrices = {};
        (data.items || []).forEach(function (item) { customerPrices[item.name] = item.price; });
        if (!result.ok) {
          taxIdHelp.textContent = '查不到這個統編，請手動填寫公司名稱與地址';
          return;
        }
        fillIfEmpty('companyName', data.companyName);
        fillIfEmpty('companyAddress', data.companyAddress);
        fillIfEmpty('cname', data.cname);
        fillIfEmpty('cphone', data.cphone);
        taxIdHelp.textContent = data.source === 'history'
          ? '已帶入上次報價 ' + data.quote_no + '（' + data.quote_date.replace(/-/g, '/') + '）的資料'
          : '已帶入經濟部商業司登記資料';
      })
      .catch(function () { taxIdHelp.textContent = '暫時查不到公司資料，請手動填寫'; lastLookup = ''; });
  }

  taxId.addEventListener('input', lookupCustomer);
  taxId.addEventListener('change', lookupCustomer);

  // ---- 從報價過的品項加入：把「品名,數量,上次單價」加成新的一行，單價之後還能改 ----
  var itemName = document.getElementById('item-name');
  var itemAdd = document.getElementById('item-add');

  function knownPrice(name) {
    if (Object.prototype.hasOwnProperty.call(customerPrices, name)) return customerPrices[name];
    var options = document.getElementById('item-names').options;
    for (var i = 0; i < options.length; i++) {
      if (options[i].value === name) return options[i].dataset.price;
    }
    return '';
  }

  function appendLine(textarea, line) {
    var current = textarea.value.replace(/\s+$/, '');
    textarea.value = current ? current + '\n' + line : line;
  }

  if (itemAdd) {
    itemAdd.addEventListener('click', function () {
      var name = itemName.value.trim();
      if (!name) { itemName.focus(); return; }
      var qty = document.getElementById('item-qty').value || '1';
      var price = knownPrice(name);
      appendLine(productBox, name + ',' + qty + ',' + price);
      itemName.value = '';
      document.getElementById('item-qty').value = '1';
      schedulePreview();
      if (!price) productBox.focus();  // 沒報價過的品項：讓使用者自己填單價
    });
    itemName.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); itemAdd.click(); }
    });
  }

  // ---- 常用備註 ----
  document.querySelectorAll('[data-note-preset]').forEach(function (button) {
    button.addEventListener('click', function () {
      var note = field('note');
      var text = button.dataset.notePreset;
      if (note.value.indexOf(text) === -1) appendLine(note, text);
      note.focus();
    });
  });

  // ---- 記住每位銷售員常用的選項（只存在這台電腦的瀏覽器，不含客戶資料） ----
  var REMEMBERED = ['delivery', 'cash', 'vday', 'tax'];
  var touched = {};
  REMEMBERED.forEach(function (name) {
    field(name).addEventListener('change', function () { touched[name] = true; });
  });

  function storage() {
    try { return window.localStorage; } catch (e) { return null; }
  }

  function rememberSellerDefaults() {
    var store = storage();
    if (!store) return;
    var defaults = {};
    REMEMBERED.forEach(function (name) { defaults[name] = field(name).value; });
    try {
      store.setItem('quotation:lastSeller', field('seller').value);
      store.setItem('quotation:defaults:' + field('seller').value, JSON.stringify(defaults));
    } catch (e) { /* 瀏覽器不允許儲存就算了 */ }
  }

  function setSelect(select, value) {
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) { select.value = value; return; }
    }
  }

  function applySellerDefaults() {
    var store = storage();
    if (!store) return;
    try {
      var defaults = JSON.parse(store.getItem('quotation:defaults:' + field('seller').value) || '{}');
      REMEMBERED.forEach(function (name) {
        if (!touched[name] && defaults[name]) setSelect(field(name), defaults[name]);
      });
    } catch (e) { /* 存的資料壞了就忽略 */ }
  }

  if (form.dataset.blank === 'true') {
    var store = storage();
    try {
      var lastSeller = store && store.getItem('quotation:lastSeller');
      if (lastSeller) setSelect(field('seller'), lastSeller);
    } catch (e) { /* ignore */ }
    applySellerDefaults();
    field('seller').addEventListener('change', applySellerDefaults);
  }
})();
