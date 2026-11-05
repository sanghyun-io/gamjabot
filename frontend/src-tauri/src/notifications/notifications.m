#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <UserNotifications/UserNotifications.h>
#import <dispatch/dispatch.h>
#import <stdlib.h>
#import <string.h>

// Only opaque delivery IDs cross the OS notification boundary; no task content.
@interface GamjaNotifications : NSObject <UNUserNotificationCenterDelegate>
@property(nonatomic,strong) NSMutableArray<NSString *> *clicks;
@end
@implementation GamjaNotifications
- (instancetype)init {if((self=[super init]))_clicks=[NSMutableArray array];return self;}
- (void)userNotificationCenter:(UNUserNotificationCenter *)center willPresentNotification:(UNNotification *)notification withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler { (void)center;(void)notification;completionHandler(UNNotificationPresentationOptionAlert); }
- (void)userNotificationCenter:(UNUserNotificationCenter *)center didReceiveNotificationResponse:(UNNotificationResponse *)response withCompletionHandler:(void (^)(void))completionHandler {
 (void)center;NSString *value=response.notification.request.identifier;
 if([response.actionIdentifier isEqualToString:UNNotificationDefaultActionIdentifier] && value.length==36){dispatch_async(dispatch_get_main_queue(),^{if(self.clicks.count<32)[self.clicks addObject:value];[NSApp activateIgnoringOtherApps:YES];});}
 completionHandler();
}
@end
static GamjaNotifications *delegateInstance;
static void onMain(void (^work)(void)) {if([NSThread isMainThread])work();else dispatch_sync(dispatch_get_main_queue(),work);}
void gamja_notifications_init(void) { @autoreleasepool { @try {onMain(^{if(!delegateInstance){GamjaNotifications *candidate=[GamjaNotifications new];[UNUserNotificationCenter currentNotificationCenter].delegate=candidate;delegateInstance=candidate;}});} @catch(NSException *e){(void)e;} } }
static char *copyText(NSString *s){return strdup((s?:@"unknown").UTF8String);}
static NSString *permission(UNAuthorizationStatus s){switch(s){case UNAuthorizationStatusNotDetermined:return @"not_determined";case UNAuthorizationStatusDenied:return @"denied";case UNAuthorizationStatusAuthorized:return @"authorized";default:return @"unknown";}}
char *gamja_notification_run(const char *actionRaw,const char *idRaw,const char *kindRaw) {
 @autoreleasepool { @try {
  NSString *action=actionRaw?[NSString stringWithUTF8String:actionRaw]:@"";
  if([action isEqualToString:@"take"]){__block NSString *value=@"none";onMain(^{if(delegateInstance.clicks.count){value=delegateInstance.clicks.firstObject;[delegateInstance.clicks removeObjectAtIndex:0];}});return copyText(value);}
  UNUserNotificationCenter *center=[UNUserNotificationCenter currentNotificationCenter];dispatch_semaphore_t done=dispatch_semaphore_create(0);__block NSString *out=@"unknown";
  if([action isEqualToString:@"status"]){[center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *s){out=permission(s.authorizationStatus);dispatch_semaphore_signal(done);}];}
  else if([action isEqualToString:@"request"]){[center requestAuthorizationWithOptions:UNAuthorizationOptionAlert completionHandler:^(__unused BOOL granted,NSError *error){if(error){out=@"unknown";dispatch_semaphore_signal(done);}else [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *s){out=permission(s.authorizationStatus);dispatch_semaphore_signal(done);}];}];}
  else if([action isEqualToString:@"deliver"]){NSString *identifier=idRaw?[NSString stringWithUTF8String:idRaw]:@"",*kind=kindRaw?[NSString stringWithUTF8String:kindRaw]:@"";[center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *s){if(s.authorizationStatus!=UNAuthorizationStatusAuthorized){out=@"failed";dispatch_semaphore_signal(done);return;}UNMutableNotificationContent *c=[UNMutableNotificationContent new];c.title=@"감자봇";c.body=[kind isEqualToString:@"approval"]?@"확인이 필요한 작업이 있습니다.":[kind isEqualToString:@"failure"]?@"확인이 필요한 작업 결과가 있습니다.":@"작업이 완료되었습니다.";UNNotificationRequest *request=[UNNotificationRequest requestWithIdentifier:identifier content:c trigger:nil];[center addNotificationRequest:request withCompletionHandler:^(NSError *error){out=error?@"failed":@"accepted";dispatch_semaphore_signal(done);}];}];}
  else return copyText(@"unknown");
  long timeout=[action isEqualToString:@"request"]?60:5;if(dispatch_semaphore_wait(done,dispatch_time(DISPATCH_TIME_NOW,timeout*NSEC_PER_SEC))!=0)return copyText(@"unknown");return copyText(out);
 } @catch(NSException *e){(void)e;return copyText(@"unknown");} }
}
