/** Mobile sidebar + top nav toggle (all pages) */
(function () {
    function initNav() {
        const toggle = document.getElementById('menu-toggle');
        const sidebar = document.getElementById('sidebar');
        const overlay = document.getElementById('sidebar-overlay');
        const topNav = document.getElementById('top-nav');

        if (!toggle || !sidebar) return;

        function closeMenu() {
            sidebar.classList.remove('open');
            if (overlay) overlay.classList.remove('open');
            document.body.classList.remove('mobile-nav-open');
        }

        function openMenu() {
            sidebar.classList.add('open');
            if (overlay) overlay.classList.add('open');
            document.body.classList.add('mobile-nav-open');
        }

        toggle.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            if (sidebar.classList.contains('open')) {
                closeMenu();
            } else {
                openMenu();
            }
            if (topNav) topNav.classList.toggle('open');
        });

        if (overlay) overlay.addEventListener('click', closeMenu);

        document.querySelectorAll('.sidebar-menu li').forEach(function (li) {
            li.addEventListener('click', function () {
                if (window.innerWidth <= 992) closeMenu();
            });
        });

        window.addEventListener('resize', function () {
            if (window.innerWidth > 992) closeMenu();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initNav);
    } else {
        initNav();
    }
})();
