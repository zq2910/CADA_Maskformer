# optimizer
optimizer = dict(type='SGD', lr=0.01, momentum=0.9, weight_decay=0.0005)
optimizer_config = dict()
# learning policy
lr_config = dict(policy='poly', power=0.9, min_lr=1e-4, by_epoch=False)
# runtime settings
runner = dict(type='EpochBasedRunner', max_epochs=120)
checkpoint_config = dict(by_epoch=True, interval=1)
evaluation = dict(interval=1, metric='mIoU' , save_best="auto")

# runner = dict(type='IterBasedRunner', max_iters=10000)
# checkpoint_config = dict(by_epoch=False, interval=100)
# evaluation = dict(interval=100, metric='mIoU')