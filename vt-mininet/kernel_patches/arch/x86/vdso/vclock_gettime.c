--- linux-3.16.3-clean/arch/x86/vdso/vclock_gettime.c	2014-09-17 11:22:16.000000000 -0600
+++ ./linux-3.16.3-vtmininet/arch/x86/vdso/vclock_gettime.c	2017-08-29 10:02:36.888974335 -0600
@@ -319,17 +319,19 @@ int clock_gettime(clockid_t, struct time
 
 notrace int __vdso_gettimeofday(struct timeval *tv, struct timezone *tz)
 {
-	if (likely(tv != NULL)) {
-		if (unlikely(do_realtime((struct timespec *)tv) == VCLOCK_NONE))
-			return vdso_fallback_gtod(tv, tz);
-		tv->tv_usec /= 1000;
-	}
-	if (unlikely(tz != NULL)) {
-		tz->tz_minuteswest = gtod->tz_minuteswest;
-		tz->tz_dsttime = gtod->tz_dsttime;
-	}
+    return vdso_fallback_gtod(tv, tz);
 
-	return 0;
+	/*if (likely(tv != NULL)) {*/
+		/*if (unlikely(do_realtime((struct timespec *)tv) == VCLOCK_NONE))*/
+			/*return vdso_fallback_gtod(tv, tz);*/
+		/*tv->tv_usec /= 1000;*/
+	/*}*/
+	/*if (unlikely(tz != NULL)) {*/
+		/*tz->tz_minuteswest = gtod->tz_minuteswest;*/
+		/*tz->tz_dsttime = gtod->tz_dsttime;*/
+	/*}*/
+
+	/*return 0;*/
 }
 int gettimeofday(struct timeval *, struct timezone *)
 	__attribute__((weak, alias("__vdso_gettimeofday")));