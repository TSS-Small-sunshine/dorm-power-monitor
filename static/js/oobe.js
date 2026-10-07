/**
 * oobe.js  — R65 (session-driven OOBE wizard + View Transitions API)
 *
 * Replaces the R34B/R37/R63 wizard which used ?step=N URL params +
 * in-page template literals.  R65 stores wizard state in the Flask
 * session (`session['oobe']`), so:
 *
 *   - Refresh does NOT lose state.
 *   - Skipping to a later step directly via the URL is impossible
 *     (the server clamps `session['oobe'].step` to the user's
 *     progress; the JS simply renders the section whose data-step
 *     matches that integer).
 *   - Each "下一步" click first validates the current step's data,
 *     posts to `/api/oobe/next`, then asks the server to advance.
 *
 * Steps:
 *   1. 欢迎       (welcome)
 *   2. 飞书凭证   (feishu app_id / app_secret / verification_token / encrypt_key)
 *   3. 推送配置   (webhook url + secret)
 *   4. 定时计划   (cron + timezone)
 *   5. 数据保留   (records days + monthly backups)
 *   6. Admin 账户 (username / password / password_confirm)
 *
 * View transitions wrap the section swap when the browser supports
 * `document.startViewTransition`; otherwise we fall back to the
 * existing CSS animation on `.oobe-step`.
 *
 * Components used: <dorm-toast> + <dorm-spinner> from R66.
 */

(function () {
  "use strict";

  // ---- helpers --------------------------------------------------

  /** Current step read from <body data-step> (set by Flask). */
  var currentStep = parseInt(document.body.dataset.step || "1", 10);
  if (!(currentStep >= 1 && currentStep <= 7)) currentStep = 1;

  /** Wraps a callback in document.startViewTransition when available. */
  function withViewTransition(callback) {
    if (typeof document.startViewTransition === "function") {
      try {
        document.startViewTransition(callback);
        return;
      } catch (e) {
        /* fall through */
      }
    }
    callback();
  }

  /** Show a toast via the R66 <dorm-toast> component. */
  function showToast(msg, kind, duration) {
    var region = document.getElementById("toast_region");
    if (!region) return;
    var t = document.createElement("dorm-toast");
    t.setAttribute("kind", kind || "info");
    t.setAttribute("duration", String(duration == null ? 3500 : duration));
    t.textContent = msg;
    region.appendChild(t);
    return t;
  }

  /** Toggle the global spinner (R66 <dorm-spinner>). */
  function setBusy(busy) {
    var s = document.getElementById("global_spinner");
    if (!s) return;
    if (busy) {
      s.setAttribute("spinning", "");
      s.removeAttribute("hidden");
    } else {
      s.removeAttribute("spinning");
      s.setAttribute("hidden", "");
    }
  }

  /** POST JSON, throw on !ok.  Reads X-CSRF-Token from meta tag. */
  function postJson(url, payload) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    var csrf = meta ? meta.getAttribute("content") || "" : "";
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
      },
      body: JSON.stringify(payload || {}),
    }).then(function (resp) {
      return resp.json().then(function (json) {
        if (!resp.ok || (json && json.ok === false)) {
          var err = new Error(
            (json && (json.error || json.detail)) || ("HTTP " + resp.status)
          );
          err.status = resp.status;
          err.payload = json;
          throw err;
        }
        return json;
      });
    });
  }

  /** Update the progress-dot row to reflect current / done states. */
  function paintProgress() {
    var dots = document.querySelectorAll("#progress .oobe-progress-dot");
    dots.forEach(function (d) {
      var n = parseInt(d.dataset.step || "0", 10);
      d.classList.remove("active", "done");
      if (n < currentStep) d.classList.add("done");
      else if (n === currentStep) d.classList.add("active");
    });
  }

  /** Show only the section whose data-step == currentStep. */
  function showCurrentStepSection() {
    var sections = document.querySelectorAll(".oobe-step");
    sections.forEach(function (s) {
      var n = parseInt(s.dataset.step || "0", 10);
      if (n === currentStep) s.removeAttribute("hidden");
      else s.setAttribute("hidden", "");
    });
  }

  /** Update prev / next / skip / finish button visibility. */
  function paintActions() {
    var prev = document.getElementById("prev_btn");
    var next = document.getElementById("next_btn");
    var skip = document.getElementById("skip_btn");
    var finish = document.getElementById("finish_btn");

    prev.hidden = currentStep <= 1 || currentStep >= 7;

    // Step 7 = completion screen; finish button only.
    if (currentStep >= 7) {
      next.hidden = true;
      skip.hidden = true;
      finish.hidden = false;
      return;
    }
    finish.hidden = true;

    // Step 5 (data retention) is the only optional step.
    skip.hidden = currentStep !== 5;

    // Last configurable step → primary button reads "创建账户".
    if (currentStep === 6) {
      next.textContent = "创建账户 →";
    } else if (currentStep === 1) {
      next.textContent = "开始设置 →";
    } else {
      next.textContent = "下一步 →";
    }
    next.hidden = false;
  }

  // ---- step validators -----------------------------------------

  /** Read current step's form values into an object. */
  function collectStepData(step) {
    function val(id) {
      var el = document.getElementById(id);
      return el ? (el.value || "").trim() : "";
    }
    if (step === 2) {
      return {
        app_id: val("feishu_app_id"),
        app_secret: val("feishu_app_secret"),
        verification_token: val("feishu_verification_token"),
        encrypt_key: val("feishu_encrypt_key"),
      };
    }
    if (step === 3) {
      return { url: val("webhook_url"), secret: val("webhook_secret") };
    }
    if (step === 4) {
      return { cron_expr: val("cron_expr"), timezone: val("tz_select") };
    }
    if (step === 5) {
      return {
        records_days: parseInt(val("records_days") || "0", 10),
        monthly_backups: parseInt(val("monthly_backups") || "0", 10),
      };
    }
    if (step === 6) {
      return {
        username: val("admin_username"),
        password: document.getElementById("admin_password").value || "",
        password_confirm:
          document.getElementById("admin_password_confirm").value || "",
      };
    }
    return {};
  }

  /** Client-side validation.  Returns null on OK, or an error message. */
  function validateStepClient(step, data) {
    if (step === 2) {
      if (!data.app_id) return "请填写 App ID";
      if (!data.app_secret) return "请填写 App Secret";
      return null;
    }
    if (step === 3) {
      if (!data.url) return "请填写 Webhook URL";
      if (!/^https?:\/\//.test(data.url))
        return "Webhook URL 必须以 http(s):// 开头";
      return null;
    }
    if (step === 4) {
      if (!data.cron_expr) return "请填写 Cron 表达式";
      var parts = data.cron_expr.trim().split(/\s+/);
      if (parts.length !== 5)
        return "Cron 必须有 5 字段 (分 时 日 月 周)";
      return null;
    }
    if (step === 5) {
      if (!data.records_days || data.records_days < 5)
        return "Records 保留天数必须 ≥ 5";
      if (!data.monthly_backups || data.monthly_backups < 1)
        return "月备份份数必须 ≥ 1";
      return null;
    }
    if (step === 6) {
      if (!data.username) return "请填写用户名";
      if (!/^[A-Za-z0-9_.\-]{2,32}$/.test(data.username))
        return "用户名只能含字母 / 数字 / _ . -, 2-32 字符";
      var strength = checkPasswordStrength(data.password);
      if (strength !== "ok")
        return "密码强度不足:" + (strengthMessages[strength] || strength);
      if (data.password !== data.password_confirm) return "两次密码不一致";
      return null;
    }
    return null;
  }

  // ---- password strength ---------------------------------------

  var strengthMessages = {
    short: "长度 < 8",
    lower: "缺少小写字母",
    upper: "缺少大写字母",
    digit: "缺少数字",
    special: "缺少特殊字符",
    ok: "通过",
  };
  var strengthColors = {
    short: "var(--err)",
    lower: "var(--err)",
    upper: "var(--err)",
    digit: "var(--err)",
    special: "var(--warn)",
    ok: "var(--ok)",
  };
  var strengthPct = {
    short: "10%",
    lower: "30%",
    upper: "50%",
    digit: "70%",
    special: "85%",
    ok: "100%",
  };

  /** Mirror of web.py's _validate_password_strength(). */
  function checkPasswordStrength(pwd) {
    if (!pwd || pwd.length < 8) return "short";
    if (!/[a-z]/.test(pwd)) return "lower";
    if (!/[A-Z]/.test(pwd)) return "upper";
    if (!/[0-9]/.test(pwd)) return "digit";
    if (!/[!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?~`]/.test(pwd))
      return "special";
    return "ok";
  }

  function paintPasswordStrength(pwd) {
    var meter = document.getElementById("strength_meter");
    var bar = document.getElementById("strength_bar");
    var label = document.getElementById("strength_label");
    if (!meter || !bar || !label) return;
    if (!pwd) {
      meter.hidden = true;
      return;
    }
    meter.hidden = false;
    var s = checkPasswordStrength(pwd);
    bar.style.setProperty("--strength-pct", strengthPct[s]);
    bar.style.setProperty("--strength-color", strengthColors[s]);
    label.textContent = strengthMessages[s] || s;
    label.style.color = strengthColors[s];
  }

  // ---- per-step action handlers --------------------------------

  /** Persist step data to session. */
  function saveState(step, data) {
    return postJson("/api/oobe/save-state", { step: step, data: data });
  }

  /** Ask the server to advance from `step` to `step+1`. */
  function advance(step) {
    return postJson("/api/oobe/next", { step: step }).then(function (json) {
      return json.step;
    });
  }

  function goPrev() {
    if (currentStep <= 1) return;
    setBusy(true);
    postJson("/api/oobe/prev", { step: currentStep })
      .then(function (json) {
        var target = json.step || Math.max(1, currentStep - 1);
        switchToStep(target);
      })
      .catch(function (err) {
        showToast(err.message || "返回失败", "error");
      })
      .then(function () { setBusy(false); });
  }

  /** Validate, save, advance — single click handler for "下一步". */
  function goNext() {
    if (currentStep >= 7) return; // completion screen
    var step = currentStep;
    var data = collectStepData(step);

    var err = validateStepClient(step, data);
    if (err) {
      showToast(err, "error");
      return;
    }

    setBusy(true);
    saveState(step, data)
      .then(function () { return advance(step); })
      .then(function (newStep) {
        if (step === 6) {
          // Step 6 calls /complete instead of /next (creates admin user).
          return postJson("/api/oobe/complete", {
            username: data.username,
            password: data.password,
            password_confirm: data.password_confirm,
          }).then(function (json) {
            showToast("设置完成,欢迎使用!", "success", 4500);
            return 7; // completion screen
          });
        }
        return newStep;
      })
      .then(function (newStep) { switchToStep(newStep); })
      .catch(function (err) {
        showToast(err.message || "操作失败", "error");
      })
      .then(function () { setBusy(false); });
  }

  /** Skip an optional step (just step 5 right now). */
  function goSkip() {
    if (currentStep !== 5) return;
    setBusy(true);
    postJson("/api/oobe/skip-step", { step: 5, use_defaults: true })
      .then(function () { return advance(5); })
      .then(function (newStep) { switchToStep(newStep); })
      .catch(function (err) {
        showToast(err.message || "跳过失败", "error");
      })
      .then(function () { setBusy(false); });
  }

  /** "完成" button on step 7 — go to /admin. */
  function goFinish() {
    window.location.href = "/admin";
  }

  // ---- step transitions ----------------------------------------

  function switchToStep(target) {
    target = parseInt(target, 10);
    if (!(target >= 1 && target <= 7)) target = 1;
    var previous = currentStep;
    if (target === previous) {
      paintProgress();
      paintActions();
      return;
    }
    withViewTransition(function () {
      currentStep = target;
      document.body.setAttribute("data-step", String(target));
      showCurrentStepSection();
      paintProgress();
      paintActions();
      // Push to history so refresh keeps the URL in sync (the server
      // still ignores the param and reads from session).
      try {
        var url = "/oobe";
        if (target !== 1) url += "?step=" + target;
        window.history.replaceState({ step: target }, "", url);
      } catch (e) { /* old browser, ignore */ }
    });
  }

  // ---- test buttons --------------------------------------------

  function bindFeishuTest() {
    var btn = document.getElementById("feishu_test_btn");
    var result = document.getElementById("feishu_test_result");
    if (!btn || !result) return;
    btn.addEventListener("click", function () {
      var data = collectStepData(2);
      if (!data.app_id || !data.app_secret) {
        result.className = "oobe-test-result show err";
        result.textContent = "请先填写 App ID 和 App Secret";
        return;
      }
      result.className = "oobe-test-result show pending";
      result.textContent = "正在请求 tenant_access_token ...";
      setBusy(true);
      postJson("/api/oobe/validate-feishu", data)
        .then(function (json) {
          result.className = "oobe-test-result show ok";
          result.textContent =
            "✓ 凭证有效 (bot: " + (json.bot_name || "ok") + ")";
          showToast("飞书连接成功", "success");
        })
        .catch(function (err) {
          result.className = "oobe-test-result show err";
          result.textContent = "✗ " + (err.message || "验证失败");
        })
        .then(function () { setBusy(false); });
    });
  }

  function bindWebhookTest() {
    var btn = document.getElementById("webhook_test_btn");
    var result = document.getElementById("webhook_test_result");
    if (!btn || !result) return;
    btn.addEventListener("click", function () {
      var data = collectStepData(3);
      if (!data.url) {
        result.className = "oobe-test-result show err";
        result.textContent = "请先填写 Webhook URL";
        return;
      }
      result.className = "oobe-test-result show pending";
      result.textContent = "正在 POST 测试消息 ...";
      setBusy(true);
      postJson("/api/oobe/validate-webhook", data)
        .then(function () {
          result.className = "oobe-test-result show ok";
          result.textContent = "✓ 测试推送成功";
          showToast("Webhook 可达", "success");
        })
        .catch(function (err) {
          result.className = "oobe-test-result show err";
          result.textContent = "✗ " + (err.message || "推送失败");
        })
        .then(function () { setBusy(false); });
    });
  }

  function bindCronTest() {
    var btn = document.getElementById("cron_test_btn");
    var result = document.getElementById("cron_test_result");
    if (!btn || !result) return;
    btn.addEventListener("click", function () {
      var expr = document.getElementById("cron_expr").value || "";
      var parts = expr.trim().split(/\s+/);
      if (parts.length !== 5) {
        result.className = "oobe-test-result show err";
        result.textContent = "✗ Cron 必须有 5 字段";
        return;
      }
      // Quick client-side preview: describe the schedule in plain text.
      var desc = describeCron(parts);
      result.className = "oobe-test-result show ok";
      result.textContent = "✓ 格式合法 — " + desc;
    });
  }

  /** Tiny cron-to-English helper for the preview button. */
  function describeCron(parts) {
    var minute = parts[0], hour = parts[1];
    if (minute === "*/30" && hour === "*") return "每 30 分钟";
    if (minute === "0" && hour === "*") return "每小时整点";
    if (minute === "0" && hour === "*/2") return "每 2 小时";
    return "原始:" + parts.join(" ");
  }

  function bindPasswordStrength() {
    var pwd = document.getElementById("admin_password");
    if (!pwd) return;
    pwd.addEventListener("input", function () {
      paintPasswordStrength(pwd.value);
    });
  }

  // ---- wiring --------------------------------------------------

  function init() {
    // Initial render.
    showCurrentStepSection();
    paintProgress();
    paintActions();

    // Action buttons.
    var prev = document.getElementById("prev_btn");
    var next = document.getElementById("next_btn");
    var skip = document.getElementById("skip_btn");
    var finish = document.getElementById("finish_btn");
    if (prev) prev.addEventListener("click", goPrev);
    if (next) next.addEventListener("click", goNext);
    if (skip) skip.addEventListener("click", goSkip);
    if (finish) finish.addEventListener("click", goFinish);

    // Step-specific helpers.
    bindFeishuTest();
    bindWebhookTest();
    bindCronTest();
    bindPasswordStrength();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();