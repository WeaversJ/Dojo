/*
 * Dojo timetable widget. Paste where the timetable should appear:
 *
 *   <script src="https://<your-dojo>/embed.js" data-club="your-club" async></script>
 *
 * Optional attributes:
 *   data-weeks="2"         weeks shown at once (1 to 4)
 *   data-class="12"        one class only (its number is in the preview on Settings → Website)
 *   data-theme="dark"      for dark websites
 *   data-target="#timetable"  put the timetable inside this element instead of after the script
 *   data-title="Our timetable"  the frame's name for screen readers
 */
(function () {
    var script = document.currentScript;
    if (!script || !script.src) return;
    var club = script.getAttribute('data-club');
    if (!club) {
        if (window.console) console.warn('Dojo timetable: add data-club="your-club" to the script tag.');
        return;
    }
    var base = script.src.replace(/\/embed\.js(?:[?#].*)?$/, '');
    var origin;
    try { origin = new URL(base).origin; } catch (e) { return; }

    var params = [];
    ['weeks', 'class', 'theme'].forEach(function (key) {
        var value = script.getAttribute('data-' + key);
        if (value) params.push(key + '=' + encodeURIComponent(value));
    });

    var frame = document.createElement('iframe');
    frame.src = base + '/embed/' + encodeURIComponent(club) + '/timetable/' + (params.length ? '?' + params.join('&') : '');
    frame.title = script.getAttribute('data-title') || 'Class timetable';
    frame.loading = 'lazy';
    frame.setAttribute('scrolling', 'no');
    frame.style.cssText = 'display:block;width:100%;min-width:280px;height:520px;border:0;overflow:hidden;background:transparent;';

    var target = null;
    var selector = script.getAttribute('data-target');
    if (selector) {
        try { target = document.querySelector(selector); } catch (e) { target = null; }
    }
    if (target) target.appendChild(frame);
    else script.parentNode.insertBefore(frame, script.nextSibling);

    // The timetable reports its height as it loads, pages and filters, so the frame never scrolls.
    window.addEventListener('message', function (event) {
        if (event.origin !== origin || event.source !== frame.contentWindow) return;
        var data = event.data;
        if (data && data.dojoEmbed === 'height' && typeof data.height === 'number' && isFinite(data.height)) {
            frame.style.height = Math.max(120, Math.min(Math.ceil(data.height), 12000)) + 'px';
        }
    });
})();
